#!/usr/bin/env python3
"""Песочница: ставит пакет находки и запускает `--help` в изолированном Docker-контейнере.

Выполняется в job `run` workflow sandbox.yml: одноразовая машина GitHub Actions, job без секретов и
с `permissions: {}`. Скрипт самодостаточный (только stdlib):

    python3 -I sandbox_runner.py requests.json results.json

Изоляция:
- код пакета никогда не выполняется с доступом к сети. Установка — только готовые wheel
  (`pip --only-binary=:all:`, без setup.py) или `npm --ignore-scripts` (без install-скриптов);
  запуск `--help` — в контейнере с `--network none`;
- контейнер: не root (1000:1000), `--cap-drop ALL`, `no-new-privileges`, корень только на чтение, лимиты
  памяти/CPU/процессов, без docker.sock и без каталогов машины — только рабочая папка пакета
  (при запуске — на чтение);
- таймауты на каждый шаг; вывод обрезается и считается недоверенным текстом.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

PY_IMAGE = "python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f"
NODE_IMAGE = "node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392"
PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]{0,60}/)?[a-z0-9][a-z0-9._~-]{0,100}$")
VERSION = re.compile(r"^[0-9A-Za-z.+!_-]{1,40}$")
BIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
INSTALL_TIMEOUT = 300
RUN_TIMEOUT = 60
MAX_REQUESTS = 10

HARDEN = [
    "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--pids-limit", "256", "--memory", "1g", "--memory-swap", "1g", "--cpus", "1",
    "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=512m", "-e", "HOME=/tmp", "-w", "/tmp",
]


def docker_argv(name: str, image: str, args: list[str], *, workdir: str, network: bool, readonly: bool) -> list[str]:
    net = ["--network", "bridge", "--dns", "1.1.1.1", "--dns", "8.8.8.8"] if network else ["--network", "none"]
    return ["docker", "run", "--rm", "--name", name, *HARDEN, *net,
            "-v", f"{workdir}:/opt/pkg:{'ro' if readonly else 'rw'}", image, *args]


def run(image: str, args: list[str], *, workdir: str, network: bool, readonly: bool,
        timeout: int) -> tuple[int, str]:
    name = f"sbx-{uuid.uuid4().hex[:12]}"
    argv = docker_argv(name, image, args, workdir=workdir, network=network, readonly=readonly)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return 124, f"таймаут {timeout} с"


def tail(text: str, limit: int = 400) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)[-limit:]


def install_steps(req: dict[str, Any]) -> list[tuple[str, list[str], bool]]:
    """(образ, команда, нужна ли сеть). Ни на одном шаге с сетью не выполняется код пакета."""
    pkg, ver = req["package"], req["version"]
    if req["ecosystem"] == "pypi":
        return [
            (PY_IMAGE, ["python", "-m", "venv", "/opt/pkg/venv"], False),
            (PY_IMAGE, ["/opt/pkg/venv/bin/pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                        "--no-input", "--only-binary=:all:", f"{pkg}=={ver}"], True),
        ]
    return [(NODE_IMAGE, ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--omit=dev",
                          "--no-update-notifier", "--cache", "/tmp/.npm", "--prefix", "/opt/pkg", f"{pkg}@{ver}"],
             True)]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _read_small(path: str) -> str:
    """Текст файла без перехода по симлинкам и не больше 256 КБ."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
        return f.read(262144)


def pypi_scripts(workdir: Path, package: str) -> list[str]:
    """Команды пакета: console_scripts из entry_points.txt и файлы в bin/ из RECORD (так ставятся бинарники,
    например ruff). Файлы читаются как текст, без импорта кода и без перехода по симлинкам."""
    out: list[str] = []
    for site in (workdir / "venv" / "lib").glob("python3*/site-packages"):
        if site.is_symlink():
            continue
        for entry in os.scandir(site):
            if not entry.name.endswith(".dist-info"):
                continue
            if _norm(entry.name.removesuffix(".dist-info").rsplit("-", 1)[0]) != _norm(package):
                continue  # dist-info зависимости, а не самого пакета
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            section = None
            for line in _read_small(os.path.join(entry.path, "entry_points.txt")).splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line.strip("[]").strip()
                elif section == "console_scripts" and "=" in line:
                    out.append(line.split("=", 1)[0].strip())
            for line in _read_small(os.path.join(entry.path, "RECORD")).splitlines():
                path = line.split(",", 1)[0]
                if path.startswith("../../../bin/") and path.count("/") == 4:
                    out.append(path.rsplit("/", 1)[1])
    names = {n for n in out if BIN.match(n)}
    # сначала команда с именем пакета
    return sorted(names, key=lambda n: (_norm(n) != _norm(package), n))


def valid(req: Any) -> bool:
    if not isinstance(req, dict) or not isinstance(req.get("id"), str) or not re.match(r"^[\w.-]{1,80}$", req["id"]):
        return False
    eco, pkg, ver = req.get("ecosystem"), req.get("package"), req.get("version")
    if not isinstance(pkg, str) or not isinstance(ver, str) or not VERSION.match(ver):
        return False
    bins = req.get("bins") or []
    if not isinstance(bins, list) or not all(isinstance(b, str) and BIN.match(b) for b in bins):
        return False
    if eco == "pypi":
        return bool(PYPI_NAME.match(pkg))
    return eco == "npm" and bool(NPM_NAME.match(pkg)) and bool(bins)


def sandbox(req: dict[str, Any]) -> dict[str, Any]:
    res: dict[str, Any] = {"id": req.get("id"), "ok": False, "stage": "invalid", "command": "", "detail": ""}
    if not valid(req):
        return res
    workdir = Path(tempfile.mkdtemp(prefix="sbx-"))
    os.chmod(workdir, 0o777)  # контейнер работает от 1000:1000
    try:
        for image, args, network in install_steps(req):
            rc, out = run(image, args, workdir=str(workdir), network=network, readonly=False,
                          timeout=INSTALL_TIMEOUT)
            if rc != 0:
                res.update(stage="install", detail=tail(out))
                return res
        if req["ecosystem"] == "pypi":
            bins, image, prefix = pypi_scripts(workdir, req["package"]), PY_IMAGE, "/opt/pkg/venv/bin/"
        else:
            bins, image, prefix = list(req["bins"]), NODE_IMAGE, "/opt/pkg/node_modules/.bin/"
        if not bins:
            res.update(stage="discover", detail="у пакета нет команды для запуска")
            return res
        bin_name = bins[0]
        last = ""
        for flag in ("--help", "--version"):
            rc, out = run(image, [prefix + bin_name, flag], workdir=str(workdir), network=False, readonly=True,
                          timeout=RUN_TIMEOUT)
            res.update(stage="run", command=f"{bin_name} {flag}")
            if rc == 0 and out.strip():
                res.update(ok=True, detail=tail(out))
                return res
            last = f"код {rc}: {tail(out)}"
        res["detail"] = last
        return res
    finally:
        # файлы пакета принадлежат uid 1000 — удаляем тем же пользователем внутри контейнера
        run(PY_IMAGE, ["sh", "-c", "rm -rf /opt/pkg/* /opt/pkg/.[!.]* 2>/dev/null; true"], workdir=str(workdir),
            network=False, readonly=False, timeout=120)
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("использование: sandbox_runner.py requests.json results.json", file=sys.stderr)
        return 2
    try:
        requests = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"песочница: не прочитать заявки: {e}", file=sys.stderr)
        return 2
    results = []
    for req in (requests if isinstance(requests, list) else [])[:MAX_REQUESTS]:
        r = sandbox(req)
        print(f"песочница: {req.get('ecosystem')}:{req.get('package')} → {'ok' if r['ok'] else r['stage']} "
              f"{r['command']}")
        results.append(r)
    Path(argv[2]).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
