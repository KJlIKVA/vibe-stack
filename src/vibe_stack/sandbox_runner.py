#!/usr/bin/env python3
"""Песочница: ставит пакет находки и запускает `--help` в изолированном Docker-контейнере.

Выполняется в job `run` workflow sandbox.yml: одноразовая машина GitHub Actions, job без секретов и
с `permissions: {}`. Скрипт самодостаточный (только stdlib):

    python3 -I sandbox_runner.py requests.json results.json

Изоляция:
- контейнеры с кодом пакета работают под gVisor (`--runtime runsc`): у пакета своё ядро-песочница, ядро машины
  ему недоступно. Без gVisor скрипт ничего не запускает;
- код пакета никогда не выполняется с доступом к сети. Установка — только готовые wheel
  (`pip --only-binary=:all:`, без setup.py) или npm 11 с `--ignore-scripts` и без git-, URL- и файловых
  зависимостей (git-зависимость запускала бы вложенный `npm install` со своим `.npmrc`);
  запуск `--help` — в контейнере с `--network none`;
- у установки нет прямого выхода в интернет: контейнер во внутренней сети Docker, а наружу ходит только прокси
  этого же скрипта и только к pypi.org, files.pythonhosted.org, registry.npmjs.org (по публичным адресам);
- контейнер: не root (1000:1000), `--cap-drop ALL`, `no-new-privileges`, корень только на чтение, лимиты
  памяти/CPU/процессов, без docker.sock и без каталогов машины — только рабочая папка пакета
  (при запуске — на чтение);
- лимиты: время на шаг и на весь запуск, объём вывода, свободное место; вывод считается недоверенным текстом.

Результат пишется после каждой заявки, а перед заявкой — отметка «started»: если job оборвётся, сделанное
не потеряется, а оборвавшая его заявка будет видна (job save считает её попыткой).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PY_IMAGE = "python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f"
# node 24 с npm 11: флаги --allow-git/--allow-remote/--allow-file/--allow-directory
NODE_IMAGE = "node:24-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20"
PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]{0,60}/)?[a-z0-9][a-z0-9._~-]{0,100}$")
VERSION = re.compile(r"^[0-9A-Za-z.+!_-]{1,40}$")
BIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
ID = re.compile(r"^[\w.-]{1,80}$")
INSTALL_TIMEOUT = 240
RUN_TIMEOUT = 60
CLEANUP_TIMEOUT = 120
DEADLINE = 25 * 60           # на все заявки, после проверки окружения; шаг обрывает GitHub через 38 минут
REQUEST_BUDGET = INSTALL_TIMEOUT + 2 * RUN_TIMEOUT + CLEANUP_TIMEOUT  # худший случай одной заявки
MAX_REQUESTS = 10
MAX_OUTPUT = 1024 * 1024     # байт вывода одного контейнера; больше — контейнер останавливается
TAIL_BYTES = 4096
MIN_FREE = 2 * 1024 ** 3     # свободного места перед каждой заявкой (рабочая папка и Docker)

RUNTIME = "runsc"            # gVisor
INT_NET, EGRESS_NET = "sbx-int", "sbx-egress"  # имена сетей и их интерфейсов на машине (правила iptables в sandbox.yml)
INT_SUBNET, EGRESS_SUBNET = "172.30.0.0/24", "172.31.0.0/24"
PROXY_NAME, PROXY_IP, PROXY_PORT = "sbx-proxy", "172.30.0.2", 3128
PROXY_URL = f"http://{PROXY_IP}:{PROXY_PORT}"
PROXY_ALLOW = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
PROXY_MAX_CONN = 32
PROXY_IDLE = 60
PROXY_MAX_BYTES = 512 * 1024 * 1024
NPM_SAFE = ["--ignore-scripts", "--allow-git=none", "--allow-remote=none", "--allow-file=none",
            "--allow-directory=none", "--git=/bin/false"]

HARDEN = [
    "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--pids-limit", "256", "--memory", "1g", "--memory-swap", "1g", "--cpus", "1",
    "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=512m", "-e", "HOME=/tmp", "-w", "/tmp",
]


def docker_argv(name: str, image: str, args: list[str], *, workdir: str, network: bool | str,
                readonly: bool) -> list[str]:
    """Контейнер с кодом пакета: gVisor, без логов на диске машины; сеть — только внутренняя, через прокси.
    Строка в network — имя сети (только для проверки изоляции, код пакета так не запускается)."""
    if isinstance(network, str):
        net = ["--network", network]
    elif network:
        net = ["--network", INT_NET, "-e", f"HTTPS_PROXY={PROXY_URL}", "-e", f"HTTP_PROXY={PROXY_URL}"]
    else:
        net = ["--network", "none"]
    return ["docker", "run", "--rm", "--name", name, "--runtime", RUNTIME, "--log-driver", "none", *HARDEN, *net,
            "-v", f"{workdir}:/opt/pkg:{'ro' if readonly else 'rw'}", image, *args]


def _kill(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def run(image: str, args: list[str], *, workdir: str, network: bool | str, readonly: bool,
        timeout: int) -> tuple[int, str]:
    """Запуск с таймаутом и лимитом вывода: читаем поток сами и храним только хвост."""
    name = f"sbx-{uuid.uuid4().hex[:12]}"
    argv = docker_argv(name, image, args, workdir=workdir, network=network, readonly=readonly)
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail_buf = bytearray()
    total = 0
    too_much = threading.Event()

    def reader() -> None:
        nonlocal total
        assert p.stdout is not None
        fd = p.stdout.fileno()
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            tail_buf.extend(chunk)
            del tail_buf[:-TAIL_BYTES]
            if total > MAX_OUTPUT and not too_much.is_set():
                too_much.set()
                _kill(name)
                p.kill()  # и клиент docker: не ждём таймаута, даже если контейнер не удалился

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(name)
        p.kill()
        p.wait()
        rc = 124
    t.join(10)
    if too_much.is_set():
        return 125, f"вывод больше {MAX_OUTPUT // 1024} КБ — остановлено"
    if rc == 124:
        return 124, f"таймаут {timeout} с"
    return rc, bytes(tail_buf).decode("utf-8", "replace")


def tail(text: str, limit: int = 400) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)[-limit:]


def install_steps(req: dict[str, Any]) -> list[tuple[str, list[str], bool]]:
    """(образ, команда, нужна ли сеть). Ни на одном шаге с сетью не выполняется код пакета."""
    pkg, ver = req["package"], req["version"]
    if req["ecosystem"] == "pypi":
        return [
            (PY_IMAGE, ["python", "-m", "venv", "/opt/pkg/venv"], False),
            (PY_IMAGE, ["/opt/pkg/venv/bin/pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                        "--no-input", "--proxy", PROXY_URL, "--only-binary=:all:", f"{pkg}=={ver}"], True),
        ]
    return [(NODE_IMAGE, ["npm", "install", *NPM_SAFE, "--no-audit", "--no-fund", "--omit=dev",
                          "--no-update-notifier", "--proxy", PROXY_URL, "--https-proxy", PROXY_URL,
                          "--cache", "/tmp/.npm", "--prefix", "/opt/pkg", f"{pkg}@{ver}"], True)]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def related(bin_name: str, package: str) -> bool:
    """Команда PyPI-пакета «про него»: ruff → ruff, httpie → http, aider-chat → aider.
    То же правило проверяет job save (sandbox.py), поэтому запускаем только такие команды."""
    b, p = _norm(bin_name), _norm(package)
    return len(b) >= 2 and (p.startswith(b) or b.startswith(p))


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
    names = {n for n in out if BIN.match(n) and related(n, package)}
    # сначала команда с именем пакета
    return sorted(names, key=lambda n: (_norm(n) != _norm(package), n))


def valid(req: Any) -> bool:
    if not isinstance(req, dict) or not isinstance(req.get("id"), str) or not ID.match(req["id"]):
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


def sandbox(req: Any, *, npm_ok: bool = True) -> dict[str, Any]:
    rid = req.get("id") if isinstance(req, dict) else None
    ver = req.get("version") if isinstance(req, dict) else None
    res: dict[str, Any] = {"id": rid, "version": ver, "ok": False, "stage": "invalid", "command": "", "detail": ""}
    if not valid(req):
        return res
    if req["ecosystem"] == "npm" and not npm_ok:
        res.update(stage="install", detail="npm в образе не знает флагов безопасности — не ставим")
        return res
    workdir = Path(tempfile.mkdtemp(prefix="sbx-", dir=os.environ.get("SBX_ROOT") or None))
    os.chmod(workdir, 0o777)  # контейнер работает от 1000:1000
    try:
        for image, args, network in install_steps(req):
            rc, out = run(image, args, workdir=str(workdir), network=network, readonly=False,
                          timeout=INSTALL_TIMEOUT)
            if rc != 0 or "Unknown cli config" in out:
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
            network=False, readonly=False, timeout=CLEANUP_TIMEOUT)
        shutil.rmtree(workdir, ignore_errors=True)


# --- прокси для установки ---------------------------------------------------------------------------
def proxy_target(head: bytes) -> str | None:
    """Хост из «CONNECT host:443 HTTP/1.1», если он в списке разрешённых. Обычный HTTP не пропускаем."""
    m = re.match(rb"CONNECT ([A-Za-z0-9.-]{1,253}):443 HTTP/1\.[01]\r?$", head.split(b"\n", 1)[0])
    host = m.group(1).decode().lower() if m else ""
    return host if host in PROXY_ALLOW else None


def public_addrs(host: str) -> list[str]:
    """IPv4-адреса хоста, только если все публичные (без частных сетей, метаданных облака, localhost)."""
    ips = [str(i[4][0]) for i in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]
    return ips if ips and all(ipaddress.ip_address(ip).is_global for ip in ips) else []


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    sent = 0
    try:
        while sent < PROXY_MAX_BYTES and (data := src.recv(65536)):
            dst.sendall(data)
            sent += len(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)


def _proxy_one(client: socket.socket, slots: threading.BoundedSemaphore) -> None:
    upstream = None
    try:
        client.settimeout(PROXY_IDLE)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk or len(buf) > 16384:
                return
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        host = proxy_target(head)
        ips = public_addrs(host) if host else []
        print(f"proxy: {host or 'запрещено'} {'ok' if ips else 'отказ'}", flush=True)
        if not ips:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        upstream = socket.create_connection((ips[0], 443), timeout=PROXY_IDLE)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
        back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
        back.start()
        _pipe(client, upstream)
        back.join(PROXY_IDLE)
    except OSError:
        pass
    finally:
        for s in (client, upstream):
            if s is not None:
                s.close()
        slots.release()


def serve_proxy() -> None:
    slots = threading.BoundedSemaphore(PROXY_MAX_CONN)
    srv = socket.create_server(("0.0.0.0", PROXY_PORT))
    print(f"proxy: слушаю :{PROXY_PORT}, разрешено: {', '.join(PROXY_ALLOW)}", flush=True)
    while True:
        conn, _ = srv.accept()
        if not slots.acquire(blocking=False):
            conn.close()
            continue
        threading.Thread(target=_proxy_one, args=(conn, slots), daemon=True).start()


# --- окружение ---------------------------------------------------------------------------
def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def preflight() -> str | None:
    """Без gVisor и образов не запускаем ничего. Возвращает причину отказа или None."""
    info = _docker("info", "--format", "{{json .Runtimes}}")
    if info.returncode != 0 or RUNTIME not in info.stdout:
        return "gVisor (runsc) не подключён к Docker"
    for image in (PY_IMAGE, NODE_IMAGE):
        if _docker("pull", "-q", image, timeout=300).returncode != 0:
            return f"не скачать образ {image.split('@')[0]}"
    return None


def network_up(script: Path) -> None:
    """Внутренняя сеть для установки и прокси — единственный путь из неё наружу."""
    steps = [
        ["network", "create", "--internal", "--subnet", INT_SUBNET,
         "-o", f"com.docker.network.bridge.name={INT_NET}", INT_NET],
        ["network", "create", "--subnet", EGRESS_SUBNET, "-o", f"com.docker.network.bridge.name={EGRESS_NET}",
         EGRESS_NET],
        ["run", "-d", "--name", PROXY_NAME, *HARDEN, "--network", EGRESS_NET, "--dns", "1.1.1.1", "--dns", "8.8.8.8",
         "-v", f"{script}:/opt/runner/sandbox_runner.py:ro", PY_IMAGE,
         "python", "-I", "/opt/runner/sandbox_runner.py", "--proxy"],
        ["network", "connect", "--ip", PROXY_IP, INT_NET, PROXY_NAME],
    ]
    for args in steps:
        p = _docker(*args)
        if p.returncode != 0:
            raise RuntimeError(f"docker {args[0]} {args[1]}: {tail(p.stderr, 200)}")


def network_down() -> None:
    logs = _docker("logs", "--tail", "40", PROXY_NAME)
    lines = [ln.strip() for ln in (logs.stdout + logs.stderr).splitlines() if ln.strip()]
    if lines:
        print("журнал прокси (хвост):\n" + "\n".join("  " + tail(ln, 200) for ln in lines))
    _docker("rm", "-f", PROXY_NAME)
    _docker("network", "rm", INT_NET, EGRESS_NET)


INT_GW = INT_SUBNET.rsplit(".", 1)[0] + ".1"        # адрес машины во внутренней сети
EGRESS_GW = EGRESS_SUBNET.rsplit(".", 1)[0] + ".1"
# Проверка изоляции изнутри контейнеров перед заявками: всё, что должно быть закрыто, закрыто на самом деле.
# install — контейнер установки (внутренняя сеть, прокси); egress — сеть прокси (страховка iptables из sandbox.yml).
# Аргументы: режим, версия ядра машины, порт, который машина слушает на время проверки.
ISOLATION_PROBE = f"""
import os, socket, sys
mode, host_release, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
bad = []
def reach(h, p):
    try:
        socket.create_connection((h, p), timeout=3).close()
        return True
    except OSError:
        return False
def via_proxy(h):
    try:
        s = socket.create_connection(("{PROXY_IP}", {PROXY_PORT}), timeout=10)
        s.sendall(f"CONNECT {{h}}:443 HTTP/1.1\\r\\nHost: {{h}}:443\\r\\n\\r\\n".encode())
        return s.recv(64).split(b" ")[1].decode()
    except (OSError, IndexError):
        return "-"
if os.uname().release == host_release:
    bad.append("ядро машины, а не gVisor")
if mode == "install":
    closed = [("1.1.1.1", 443), ("169.254.169.254", 80), ("{INT_GW}", port)]
    if via_proxy("example.com") != "403":
        bad.append("прокси пускает example.com")
    if via_proxy("pypi.org") != "200":
        bad.append("прокси не пускает pypi.org")
else:
    closed = [("169.254.169.254", 80), ("168.63.129.16", 80), ("{EGRESS_GW}", port)]
    if not reach("1.1.1.1", 443):
        bad.append("у сети прокси нет интернета")
for h, p in closed:
    if reach(h, p):
        bad.append(f"{{mode}}: {{h}}:{{p}} открыт")
print("ISOLATION " + ("ok" if not bad else "; ".join(bad)))
"""


def isolation_check() -> str | None:
    """Проверка изоляции изнутри контейнеров (установки и сети прокси). Возвращает проблему или None."""
    listener = socket.create_server(("0.0.0.0", 0))  # «служба машины»: из контейнеров должна быть недоступна
    listener.settimeout(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            with contextlib.suppress(OSError):
                listener.accept()[0].close()

    threading.Thread(target=accept, daemon=True).start()
    empty = tempfile.mkdtemp(prefix="sbx-probe-", dir=os.environ.get("SBX_ROOT") or None)
    problems = []
    try:
        for mode, network in (("install", True), ("egress", EGRESS_NET)):
            rc, out = run(PY_IMAGE, ["python", "-I", "-c", ISOLATION_PROBE, mode, os.uname().release, str(port)],
                          workdir=empty, network=network, readonly=True, timeout=RUN_TIMEOUT)
            line = next((ln for ln in out.splitlines() if ln.startswith("ISOLATION ")), "")
            if rc != 0 or line != "ISOLATION ok":
                problems.append(line.removeprefix("ISOLATION ") or f"{mode}: код {rc}: {tail(out, 200)}")
    finally:
        stop.set()
        listener.close()
        shutil.rmtree(empty, ignore_errors=True)
    return "; ".join(problems) or None


def npm_flags_ok() -> bool:
    """npm на неизвестный флаг только предупреждает — проверяем, что флаги безопасности он понимает."""
    empty = tempfile.mkdtemp(prefix="sbx-npm-", dir=os.environ.get("SBX_ROOT") or None)
    try:
        rc, out = run(NODE_IMAGE, ["npm", "config", "get", "allow-git", *NPM_SAFE], workdir=empty,
                      network=False, readonly=True, timeout=RUN_TIMEOUT)
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    return rc == 0 and "Unknown" not in out and out.strip().splitlines()[-1:] == ["none"]


def low_disk() -> str | None:
    for path in (os.environ.get("SBX_ROOT") or tempfile.gettempdir(), "/"):
        free = shutil.disk_usage(path).free
        if free < MIN_FREE:
            return f"мало места в {path}: {free // 1024 ** 2} МБ"
    return None


def write_results(path: Path, results: list[dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if argv[1:] == ["--proxy"]:
        serve_proxy()
        return 0
    if len(argv) != 3:
        print("использование: sandbox_runner.py requests.json results.json", file=sys.stderr)
        return 2
    try:
        requests = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"песочница: не прочитать заявки: {e}", file=sys.stderr)
        return 2
    out = Path(argv[2])
    results: list[dict[str, Any]] = []
    write_results(out, results)
    if problem := preflight():
        print(f"песочница: {problem} — ничего не запускаю", file=sys.stderr)
        return 3
    start = time.monotonic()
    try:
        network_up(Path(__file__).resolve())
        if problem := isolation_check():
            print(f"песочница: изоляция нарушена — {problem}; ничего не запускаю", file=sys.stderr)
            return 4
        print("песочница: изоляция проверена (gVisor; у установки нет прямой сети, прокси — только реестры; "
              "машина, метаданные облака и служебный адрес Azure недоступны)")
        npm_ok = npm_flags_ok()
        if not npm_ok:
            print("песочница: npm не понимает флаги безопасности — npm-пакеты не ставлю", file=sys.stderr)
        for req in (requests if isinstance(requests, list) else [])[:MAX_REQUESTS]:
            if time.monotonic() - start > DEADLINE - REQUEST_BUDGET:
                print("песочница: время запуска вышло — остальные заявки в следующий раз")
                break
            if why := low_disk():
                print(f"песочница: {why} — остальные заявки в следующий раз")
                break
            d = req if isinstance(req, dict) else {}
            results.append({"id": d.get("id"), "version": d.get("version"), "ok": False, "stage": "started",
                            "command": "", "detail": ""})
            write_results(out, results)
            try:
                r = sandbox(req, npm_ok=npm_ok)
            except Exception as e:  # одна заявка не должна ронять остальные
                r = {**results[-1], "stage": "error", "detail": f"{type(e).__name__}: {tail(str(e), 200)}"}
            results[-1] = r
            write_results(out, results)
            print(f"песочница: {d.get('ecosystem')}:{d.get('package')} → {'ok' if r['ok'] else r['stage']} "
                  f"{r['command']}")
    finally:
        network_down()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
