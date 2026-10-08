"""Песочница (фаза 3): какие находки запускать и как пометить пост.

Кто что делает:
- сбор (этот модуль): находит в тексте первоисточника команду установки (pip/pipx/uv/npm/npx), проверяет
  пакет в реестре (PyPI или npm) и ставит заявку. Пакет принимается, только если реестр ссылается на тот же
  репозиторий GitHub, что и пост, и это подтверждено: provenance пакета (PEP 740 / npm provenance) указывает
  на этот репозиторий, а без provenance — сам репозиторий объявляет это имя пакета (package.json,
  pyproject.toml, setup.cfg в корне). Так README или чужой пакет со ссылкой на репозиторий не подсунут
  чужой код под пометку «Запущено»;
- .github/workflows/sandbox.yml: job без секретов и без прав ставит пакет и запускает `--help`
  в изолированном контейнере (sandbox_runner.py), job save записывает результат (`sandbox-apply`);
- публикация: ждёт результат до sandbox.max_wait_minutes. Если запуск прошёл, код (а не модель) добавляет
  строку «🧪 Запущено в песочнице». Если не прошёл или не успел, пост выходит как обычно, без пометки.

Команды строит код из имени пакета и версии, проверенных регулярками и реестром. Текст из README не исполняется.
"""

from __future__ import annotations

import base64
import configparser
import html
import json
import logging
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .models import PostRecord
from .sandbox_runner import _norm, related
from .timeutil import parse_dt
from .urls import github_repo

log = logging.getLogger(__name__)

# имена и версии — те же правила, что в sandbox_runner.py (он проверяет их ещё раз)
PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]{0,60}/)?[a-z0-9][a-z0-9._~-]{0,100}$")
VERSION = re.compile(r"^[0-9A-Za-z.+!_-]{1,40}$")
BIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60} --(help|version)$")
STAGES = {"invalid", "install", "discover", "run", "error"}
MAX_MANIFEST = 512 * 1024

_FLAGS = r"((?:-{1,2}[A-Za-z][\w-]*\s+)*)"
_START = r"(?:^|[\s`$>(])"
_NPM_PKG = r"(@?[a-z0-9][a-z0-9._~-]*(?:/[a-z0-9][a-z0-9._~-]*)?)"
# после имени — конец, пробел или версия/extras; «github:owner/repo», пути и URL пакетами не считаются
_END = r"(?=$|[\s`'\")\[=<>~!@;,])"
INSTALL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pypi", re.compile(_START + r"(?:pip3?|python3?\s+-m\s+pip|uv\s+pip|pipx|uv\s+tool)\s+install\s+" + _FLAGS
                        + r"([A-Za-z0-9][A-Za-z0-9._-]*)" + _END, re.MULTILINE)),
    ("pypi", re.compile(_START + r"(?:uvx|pipx\s+run)\s+" + _FLAGS + r"([A-Za-z0-9][A-Za-z0-9._-]*)" + _END,
                        re.MULTILINE)),
    ("npm", re.compile(_START + r"npm\s+(?:install|i)\s+" + _FLAGS + _NPM_PKG + _END, re.MULTILINE)),
    ("npm", re.compile(_START + r"npx\s+" + _FLAGS + _NPM_PKG + _END, re.MULTILINE)),
]
NOT_A_PACKAGE = {"pip", "uv", "setuptools", "wheel", "requirements", "requirements.txt", "npm", "pnpm", "yarn"}


@dataclass
class Resolved:
    ecosystem: str
    package: str
    version: str
    bins: list[str]
    origin: str | None = None  # provenance | manifest; None — без сверки с репозиторием (ручная проверка)


def extract_install_candidates(text: str) -> list[tuple[str, str]]:
    """(экосистема, имя пакета) из команд установки в тексте — в порядке появления, без повторов."""
    found: list[tuple[int, str, str]] = []
    for eco, rx in INSTALL_PATTERNS:
        for m in rx.finditer(text):
            name = m.group(2).rstrip(".")
            if name.lower() in NOT_A_PACKAGE:
                continue
            if eco == "npm" and "/" in name and not name.startswith("@"):
                continue  # owner/repo с GitHub, а не пакет npm
            found.append((m.start(2), eco, name))
    out: list[tuple[str, str]] = []
    for _, eco, name in sorted(found):
        if (eco, name.lower()) not in {(e, n.lower()) for e, n in out}:
            out.append((eco, name))
    return out


def _repos_in(values: list[Any]) -> set[str]:
    """owner/repo GitHub из ссылок реестра (https://, git+https://, git@github.com:…, github:owner/repo)."""
    out = set()
    for v in values:
        if not isinstance(v, str) or not v:
            continue
        u = v.strip()
        u = re.sub(r"^git\+", "", u)
        u = re.sub(r"^git@github\.com:", "https://github.com/", u)
        u = re.sub(r"^(git|ssh)://(git@)?github\.com/", "https://github.com/", u)
        u = re.sub(r"^github:", "https://github.com/", u)
        if repo := github_repo(u):
            out.add(repo.removesuffix(".git"))
    return out


class Registry:
    """Проверка пакета в реестре: существует, ссылается на репозиторий поста, происхождение подтверждено,
    есть что запускать."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def resolve(self, ecosystem: str, name: str, repo: str | None) -> Resolved | None:
        if ecosystem == "pypi":
            return self._pypi(name, repo)
        if ecosystem == "npm":
            return self._npm(name, repo)
        return None

    def _fetch(self, url: str) -> httpx.Response | None:
        """GET с редиректами (PyPI переадресует ненормализованные имена), но только в пределах того же хоста."""
        r = self.client.get(url, headers={"Accept": "application/json"}, follow_redirects=True)
        if urlsplit(str(r.url)).hostname != urlsplit(url).hostname:
            raise ValueError(f"редирект на другой хост: {urlsplit(str(r.url)).hostname}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r

    def _get(self, url: str) -> dict[str, Any] | None:
        r = self._fetch(url)
        data = r.json() if r is not None else None
        return data if isinstance(data, dict) else None

    def _text(self, url: str) -> str | None:
        r = self._fetch(url)
        return r.text if r is not None and len(r.content) <= MAX_MANIFEST else None

    def _pypi(self, name: str, repo: str | None) -> Resolved | None:
        if not PYPI_NAME.fullmatch(name):
            return None
        data = self._get(f"https://pypi.org/pypi/{quote(name)}/json")
        if not data:
            return None
        info = data.get("info") or {}
        links = [info.get("home_page"), info.get("project_url"), info.get("download_url"),
                 *(info.get("project_urls") or {}).values()]
        if repo is not None and repo not in _repos_in(links):
            log.info("песочница: pypi %s не ссылается на %s — не запускаем", name, repo)
            return None
        version = str(info.get("version") or "")
        wheels = [f for f in data.get("urls") or [] if f.get("packagetype") == "bdist_wheel" and not f.get("yanked")]
        if not VERSION.fullmatch(version) or not wheels:
            return None  # без wheel пришлось бы выполнять setup.py при установке — так не делаем
        package = str(info.get("name") or name)
        origin = None
        if repo is not None:
            prov = self._pypi_provenance(package, version, str(wheels[0].get("filename") or ""))
            origin = _origin("pypi", package, repo, prov, lambda: self._manifest_names("pypi", repo))
            if origin is None:
                return None
        return Resolved("pypi", package, version, [], origin)

    def _npm(self, name: str, repo: str | None) -> Resolved | None:
        if not NPM_NAME.fullmatch(name):
            return None
        data = self._get(f"https://registry.npmjs.org/{quote(name, safe='@')}/latest")
        if not data:
            return None
        rep = data.get("repository")
        links = [rep.get("url") if isinstance(rep, dict) else rep, data.get("homepage"),
                 (data.get("bugs") or {}).get("url") if isinstance(data.get("bugs"), dict) else None]
        if repo is not None and repo not in _repos_in(links):
            log.info("песочница: npm %s не ссылается на %s — не запускаем", name, repo)
            return None
        version = str(data.get("version") or "")
        raw_bin = data.get("bin")
        if isinstance(raw_bin, str):
            bins = [name.split("/")[-1]]
        elif isinstance(raw_bin, dict):
            bins = [b for b in raw_bin if isinstance(b, str)]
        else:
            bins = []
        bins = [b for b in bins if BIN.fullmatch(b)][:5]
        if not VERSION.fullmatch(version) or not bins:
            return None  # библиотека без команды: запускать нечего
        origin = None
        if repo is not None:
            origin = _origin("npm", name, repo, self._npm_provenance(data),
                             lambda: self._manifest_names("npm", repo))
            if origin is None:
                return None
        return Resolved("npm", name, version, bins, origin)

    # --- происхождение пакета ---------------------------------------------------------------------------
    # Подписи Sigstore здесь не проверяются: PyPI и npm сами проверяют provenance при публикации, мы доверяем
    # реестру (как и при установке). Сверяем только, из какого репозитория пакет собран.
    def _pypi_provenance(self, name: str, version: str, filename: str) -> set[str] | None:
        """Репозитории GitHub из provenance файла (PEP 740). None — provenance нет."""
        if not filename:
            return None
        data = self._get(f"https://pypi.org/integrity/{quote(name)}/{quote(version)}/{quote(filename)}/provenance")
        repos = set()
        for bundle in (data or {}).get("attestation_bundles") or []:
            pub = bundle.get("publisher") if isinstance(bundle, dict) else None
            ok = isinstance(pub, dict) and pub.get("kind") == "GitHub" and isinstance(pub.get("repository"), str)
            repos.add(str(pub["repository"]).lower() if ok else "")  # другой издатель — не наш репозиторий
        return repos or None

    def _npm_provenance(self, data: dict[str, Any]) -> set[str] | None:
        """Репозитории GitHub из SLSA provenance пакета npm (`dist.attestations`). None — provenance нет."""
        att = (data.get("dist") or {}).get("attestations") if isinstance(data.get("dist"), dict) else None
        url = att.get("url") if isinstance(att, dict) else None
        if not isinstance(url, str) or not url.startswith("https://registry.npmjs.org/"):
            return None
        repos = set()
        for a in (self._get(url) or {}).get("attestations") or []:
            if not isinstance(a, dict) or "slsa.dev/provenance" not in str(a.get("predicateType")):
                continue
            try:
                stmt = json.loads(base64.b64decode(a["bundle"]["dsseEnvelope"]["payload"]))
                pred = stmt["predicate"]
            except (KeyError, TypeError, ValueError):
                continue
            # SLSA v1: buildDefinition.externalParameters.workflow.repository; v0.2: invocation.configSource.uri
            wf = ((pred.get("buildDefinition") or {}).get("externalParameters") or {}).get("workflow") or {}
            src = wf.get("repository") or ((pred.get("invocation") or {}).get("configSource") or {}).get("uri")
            repos.add(github_repo(re.sub(r"^git\+", "", str(src or "")).split("@")[0]) or "")
        return repos or None

    def _manifest_names(self, ecosystem: str, repo: str) -> set[str]:
        """Имена пакета, которые объявляет сам репозиторий в корне (ветка по умолчанию)."""
        base = f"https://raw.githubusercontent.com/{repo}/HEAD/"
        if ecosystem == "npm":
            try:
                pkg = json.loads(self._text(base + "package.json") or "null")
            except ValueError:
                return set()
            if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str) or pkg.get("private") is True:
                return set()  # private: владелец в npm не публикует — пакет с таким именем не его
            return {pkg["name"]}
        names: set[str] = set()
        if text := self._text(base + "pyproject.toml"):
            try:
                t = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                t = {}
            project, poetry = t.get("project") or {}, (t.get("tool") or {}).get("poetry") or {}
            classifiers = [*(project.get("classifiers") or []), *(poetry.get("classifiers") or [])]
            if any(str(c).startswith("Private ::") for c in classifiers):
                return set()  # владелец в PyPI не публикует — пакет с таким именем не его
            for n in (project.get("name"), poetry.get("name")):
                if isinstance(n, str):
                    names.add(_norm(n))
        if not names and (text := self._text(base + "setup.cfg")):
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read_string(text)
                if n := cp.get("metadata", "name", fallback=""):
                    names.add(_norm(n))
            except configparser.Error:
                pass
        return names


def _origin(ecosystem: str, package: str, repo: str, provenance: set[str] | None, manifest: Any) -> str | None:
    """Чем подтверждено, что пакет из репозитория поста. provenance из другого репозитория — отказ без вариантов."""
    if provenance is not None:
        if repo in provenance:
            return "provenance"
        log.info("песочница: %s %s собран не из %s (provenance: %s) — не запускаем", ecosystem, package, repo,
                 ", ".join(sorted(provenance)) or "—")
        return None
    names = manifest()
    if (_norm(package) if ecosystem == "pypi" else package) in names:
        return "manifest"
    log.info("песочница: %s %s — нет provenance, и %s не объявляет этот пакет — не запускаем", ecosystem, package,
             repo)
    return None


def plan(rt: Any, *, candidate_id: str, url: str, rubric: str, doc_text: str) -> str | None:
    """Ставит заявку в песочницу для поста из очереди. Возвращает «экосистема:пакет» или None."""
    cfg = rt.cfg.sandbox
    registry = getattr(rt, "registry", None)
    if not cfg.enabled or rubric not in cfg.rubrics or registry is None:
        return None
    repo = github_repo(url)
    if not repo:
        return None
    tried = 0
    for eco, name in extract_install_candidates(doc_text):
        if eco not in cfg.ecosystems:
            continue
        tried += 1
        if tried > 4:
            break
        try:
            res = registry.resolve(eco, name, repo)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("песочница: реестр %s недоступен для %s: %s", eco, name, type(e).__name__)
            continue
        if res and cfg.require_provenance and res.origin != "provenance":
            log.info("песочница: %s %s без provenance — по настройке не запускаем", eco, name)
            continue
        if res:
            rt.state.add_sandbox_request(candidate_id=candidate_id, repo=repo, ecosystem=res.ecosystem,
                                         package=res.package, version=res.version, bins=res.bins,
                                         origin=res.origin, now=rt.now())
            return f"{res.ecosystem}:{res.package}"
    return None


# --- публикация ---------------------------------------------------------------------------
def waiting(rt: Any, post: PostRecord, now: datetime) -> bool:
    """Результат песочницы ещё ждём (заявка свежее sandbox.max_wait_minutes)."""
    row = rt.state.sandbox_row(post.candidate_id)
    if row is None or row["status"] != "pending":
        return False
    requested = parse_dt(row["requested_at"])
    return requested is not None and now - requested < timedelta(minutes=rt.cfg.sandbox.max_wait_minutes)


def mark_line(row: Any) -> str:
    return f"🧪 Запущено в песочнице: установка и <code>{html.escape(row['command'])}</code>"


def with_mark(rt: Any, post: PostRecord) -> str:
    """HTML поста с пометкой «Запущено», если запуск в песочнице прошёл. Пометку ставит только код."""
    row = rt.state.sandbox_row(post.candidate_id)
    if row is None or row["status"] != "ok" or not COMMAND.fullmatch(row["command"] or ""):
        return post.html
    lines = post.html.rstrip("\n").split("\n")
    at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("✅")), len(lines))
    out = "\n".join([*lines[:at], mark_line(row), *lines[at:]])
    return out if len(out) <= 4096 else post.html


# --- обмен с workflow ---------------------------------------------------------------------------
def export_requests(state: Any, limit: int, *, now: datetime, max_age_hours: int,
                    max_attempts: int) -> list[dict[str, Any]]:
    """Ожидающие заявки: свежие и без лишних попыток. Заявку, чей пост уже вышел, публикация закрывает сама."""
    rows = state.sandbox_pending(limit, since=now - timedelta(hours=max_age_hours), max_attempts=max_attempts)
    return [{"id": r["candidate_id"], "ecosystem": r["ecosystem"], "package": r["package"],
             "version": r["version"], "bins": json.loads(r["bins"] or "[]")} for r in rows]


def manual_request(registry: Registry, spec: str) -> list[dict[str, Any]]:
    """Проверочная заявка из ручного запуска workflow: «pypi:ruff» или «npm:cowsay». Репозиторий не сверяется."""
    eco, _, name = spec.partition(":")
    res = registry.resolve(eco.strip(), name.strip(), None)
    if res is None:
        raise SystemExit(f"песочница: {spec} — пакета нет, нет wheel или нечего запускать")
    rid = re.sub(r"[^\w.-]", "_", f"test-{res.ecosystem}-{res.package}")[:80]  # @scope/name → _scope_name
    return [{"id": rid, "ecosystem": res.ecosystem, "package": res.package, "version": res.version, "bins": res.bins}]


def _clean(text: Any, limit: int) -> str:
    s = re.sub(r"[^\S\n]+", " ", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or "")))
    return s[-limit:]


def _load_list(path: Path) -> list[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def command_ok(command: str, row: Any) -> bool:
    """Команда в пометке: формат «<bin> --help|--version», bin — из заявки (npm) или про этот пакет (PyPI)."""
    if not COMMAND.fullmatch(command):
        return False
    bin_name = command.split(" ", 1)[0]
    if row["ecosystem"] == "npm":
        return bin_name in json.loads(row["bins"] or "[]")
    return related(bin_name, row["package"])


def charge_lost_run(state: Any, requests_path: Path, now: datetime, *, max_attempts: int) -> str | None:
    """Результатов нет совсем (машину job run потеряли). Попытку получает первая заявка запуска: если машину
    роняет она, после max_attempts она перестанет запускаться, а заявки с меньшим числом попыток пойдут первыми."""
    for q in _load_list(requests_path):
        if not (isinstance(q, dict) and isinstance(q.get("id"), str) and isinstance(q.get("version"), str)):
            continue
        row = state.sandbox_request(q["id"], q["version"])
        if row is None or row["status"] != "pending":
            continue
        if state.sandbox_attempt(q["id"], q["version"]) >= max_attempts:
            state.finish_sandbox(q["id"], q["version"], ok=False, stage="error", command="",
                                 detail="запуск песочницы терялся на этой заявке", now=now)
        return q["id"]
    return None


def apply_results(state: Any, results_path: Path, requests_path: Path, now: datetime, *,
                  max_attempts: int) -> dict[str, int]:
    """Записывает результаты песочницы. Job с чужим кодом мог подменить results.json, поэтому принимаются только
    ответы на заявки этого запуска (requests.json от job plan), каждая один раз, в строгом формате."""
    counts = {"ok": 0, "failed": 0, "retry": 0, "ignored": 0}
    asked = {(q["id"], q["version"]) for q in _load_list(requests_path)
             if isinstance(q, dict) and isinstance(q.get("id"), str) and isinstance(q.get("version"), str)}
    done: set[tuple[str, str]] = set()
    for r in _load_list(results_path):
        rid, ver = (r.get("id"), r.get("version")) if isinstance(r, dict) else (None, None)
        key = (rid, ver) if isinstance(rid, str) and isinstance(ver, str) else None
        row = state.sandbox_request(*key) if key in asked and key not in done else None
        if key is None or row is None or row["status"] != "pending":
            counts["ignored"] += 1
            continue
        done.add(key)
        if r.get("stage") == "started":  # запуск оборвался на этой заявке
            if state.sandbox_attempt(*key) < max_attempts:
                counts["retry"] += 1
                continue
            state.finish_sandbox(*key, ok=False, stage="error", command="",
                                 detail="запуск песочницы обрывался на этой заявке", now=now)
            counts["failed"] += 1
            continue
        ok = r.get("ok") is True
        stage = r.get("stage") if isinstance(r.get("stage"), str) and r["stage"] in STAGES else "invalid"
        command = r.get("command") if isinstance(r.get("command"), str) else ""
        if not command_ok(command, row):
            ok, stage = (False, "invalid") if ok else (False, stage)
            command = ""
        if state.finish_sandbox(*key, ok=ok, stage=stage, command=command, detail=_clean(r.get("detail"), 500),
                                now=now):
            counts["ok" if ok else "failed"] += 1
    return counts
