"""Песочница (фаза 3): какие находки запускать и как пометить пост.

Кто что делает:
- сбор (этот модуль): находит в тексте первоисточника команду установки (pip/pipx/uv/npm/npx), проверяет
  пакет в реестре (PyPI или npm) и ставит заявку. Пакет принимается, только если реестр ссылается на тот же
  репозиторий GitHub, что и пост: так README не подсунет чужой пакет;
- .github/workflows/sandbox.yml: job без секретов и без прав ставит пакет и запускает `--help`
  в изолированном контейнере (sandbox_runner.py), job save записывает результат (`sandbox-apply`);
- публикация: ждёт результат до sandbox.max_wait_minutes. Если запуск прошёл, код (а не модель) добавляет
  строку «🧪 Запущено в песочнице». Если не прошёл или не успел, пост выходит как обычно, без пометки.

Команды строит код из имени пакета и версии, проверенных регулярками и реестром. Текст из README не исполняется.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .models import PostRecord
from .timeutil import parse_dt
from .urls import github_repo

log = logging.getLogger(__name__)

# имена и версии — те же правила, что в sandbox_runner.py (он проверяет их ещё раз)
PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]{0,60}/)?[a-z0-9][a-z0-9._~-]{0,100}$")
VERSION = re.compile(r"^[0-9A-Za-z.+!_-]{1,40}$")
BIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60} --(help|version)$")
STAGES = {"invalid", "install", "discover", "run"}

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
    """Проверка пакета в реестре: существует, ссылается на репозиторий поста, есть что запускать."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def resolve(self, ecosystem: str, name: str, repo: str | None) -> Resolved | None:
        if ecosystem == "pypi":
            return self._pypi(name, repo)
        if ecosystem == "npm":
            return self._npm(name, repo)
        return None

    def _get(self, url: str) -> dict[str, Any] | None:
        r = self.client.get(url, headers={"Accept": "application/json"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else None

    def _pypi(self, name: str, repo: str | None) -> Resolved | None:
        if not PYPI_NAME.match(name):
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
        if not VERSION.match(version) or not wheels:
            return None  # без wheel пришлось бы выполнять setup.py при установке — так не делаем
        return Resolved("pypi", str(info.get("name") or name), version, [])

    def _npm(self, name: str, repo: str | None) -> Resolved | None:
        if not NPM_NAME.match(name):
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
        bins = [b for b in bins if BIN.match(b)][:5]
        if not VERSION.match(version) or not bins:
            return None  # библиотека без команды: запускать нечего
        return Resolved("npm", name, version, bins)


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
        if res:
            rt.state.add_sandbox_request(candidate_id=candidate_id, repo=repo, ecosystem=res.ecosystem,
                                         package=res.package, version=res.version, bins=res.bins, now=rt.now())
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
    if row is None or row["status"] != "ok" or not COMMAND.match(row["command"] or ""):
        return post.html
    lines = post.html.rstrip("\n").split("\n")
    at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("✅")), len(lines))
    out = "\n".join([*lines[:at], mark_line(row), *lines[at:]])
    return out if len(out) <= 4096 else post.html


# --- обмен с workflow ---------------------------------------------------------------------------
def export_requests(state: Any, limit: int) -> list[dict[str, Any]]:
    return [{"id": r["candidate_id"], "ecosystem": r["ecosystem"], "package": r["package"],
             "version": r["version"], "bins": json.loads(r["bins"] or "[]")} for r in state.sandbox_pending(limit)]


def manual_request(registry: Registry, spec: str) -> list[dict[str, Any]]:
    """Проверочная заявка из ручного запуска workflow: «pypi:ruff» или «npm:cowsay». Репозиторий не сверяется."""
    eco, _, name = spec.partition(":")
    res = registry.resolve(eco.strip(), name.strip(), None)
    if res is None:
        raise SystemExit(f"песочница: {spec} — пакета нет, нет wheel или нечего запускать")
    return [{"id": f"test-{res.ecosystem}-{res.package}", "ecosystem": res.ecosystem, "package": res.package,
             "version": res.version, "bins": res.bins}]


def _clean(text: Any, limit: int) -> str:
    s = re.sub(r"[^\S\n]+", " ", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or "")))
    return s[-limit:]


def apply_results(state: Any, path: Path, now: datetime) -> dict[str, int]:
    """Записывает результаты песочницы. Принимаются только ответы на ожидающие заявки и только в строгом формате."""
    counts = {"ok": 0, "failed": 0, "ignored": 0}
    try:
        results = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        results = []
    for r in results if isinstance(results, list) else []:
        row = state.sandbox_row(r.get("id") if isinstance(r, dict) else None)
        if row is None or row["status"] != "pending":
            counts["ignored"] += 1
            continue
        ok = r.get("ok") is True
        stage = r.get("stage") if r.get("stage") in STAGES else "invalid"
        command = str(r.get("command") or "")
        if ok and not COMMAND.match(command):
            ok, stage = False, "invalid"
        if not ok and not COMMAND.match(command):
            command = ""
        if state.finish_sandbox(row["candidate_id"], ok=ok, stage=stage, command=command,
                                detail=_clean(r.get("detail"), 500), now=now):
            counts["ok" if ok else "failed"] += 1
    return counts
