"""Песочница: выбор пакета, проверка реестра и происхождения, ожидание и пометка при публикации, приём результатов,
изоляция запуска."""

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from vibe_stack import sandbox, sandbox_runner
from vibe_stack.board import LocalBoard
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.llm import FakeLLM
from vibe_stack.models import PostRecord, Status
from vibe_stack.publish import run_publish
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier

ROOT = Path(__file__).resolve().parents[1]
HTML = ('🛠 <b>Tool X: что это</b>\n\nСуть.\n\n<a href="https://github.com/acme/toolx">Первоисточник</a>\n'
        '✅ Сверено с первоисточником · #инструмент')


def make_rt(cfg, tmp_path, now, registry=None) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    cfg.schedule.publish_slots = ["10:00", "14:00", "18:00"]
    cfg.schedule.slot_window_minutes = 120
    cfg.sandbox.enabled = True  # в config.yaml песочница может быть выключена — здесь проверяем её саму
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                   clock=clock, run_id="p", out_dir=tmp_path, mode="dry-run", channel_id="@c",
                   sources_factory=lambda _: [], registry=registry)


def registry(routes: dict[str, object]) -> sandbox.Registry:
    """Маршруты: dict → JSON, str → текст, ("redirect", url) → 301."""
    def handler(req: httpx.Request) -> httpx.Response:
        val = routes.get(str(req.url))
        if isinstance(val, tuple):
            return httpx.Response(301, headers={"Location": val[1]})
        if isinstance(val, str):
            return httpx.Response(200, text=val)
        return httpx.Response(200, json=val) if val is not None else httpx.Response(404, json={})

    return sandbox.Registry(httpx.Client(transport=httpx.MockTransport(handler)))


WHEEL = "toolx-1.2.0-py3-none-any.whl"
PYPI_TOOLX = {"info": {"name": "toolx", "version": "1.2.0", "home_page": "",
                       "project_urls": {"Source": "https://github.com/acme/toolx"}},
              "urls": [{"packagetype": "bdist_wheel", "yanked": False, "filename": WHEEL}]}
PYPI_JSON = "https://pypi.org/pypi/toolx/json"
PYPI_PROV = f"https://pypi.org/integrity/toolx/1.2.0/{WHEEL}/provenance"
RAW = "https://raw.githubusercontent.com/acme/toolx/HEAD/"
PYPROJECT = {RAW + "pyproject.toml": '[project]\nname = "toolx"\nversion = "1.2.0"\n'}


def pypi_provenance(repo: str, kind: str = "GitHub") -> dict:
    return {"version": 1, "attestation_bundles": [{"publisher": {"kind": kind, "repository": repo}}]}


def npm_attestations(source: str, *, v1: bool = True) -> dict:
    pred = ({"buildDefinition": {"externalParameters": {"workflow": {"repository": source}}}} if v1
            else {"invocation": {"configSource": {"uri": source}}})
    payload = base64.b64encode(json.dumps({"predicate": pred}).encode()).decode()
    return {"attestations": [
        {"predicateType": "https://github.com/npm/attestation/tree/main/specs/publish/v0.1", "bundle": {}},
        {"predicateType": "https://slsa.dev/provenance/v1", "bundle": {"dsseEnvelope": {"payload": payload}}},
    ]}


# --- какой пакет запускать ---------------------------------------------------------------------------
def test_extract_install_candidates_from_readme() -> None:
    text = """Install:
    ```
    pip install -U toolx[all]
    pip install -r requirements.txt
    npm install -g @acme/toolx-cli
    npx create-thing my-app
    uvx toolx --help
    ```"""
    assert sandbox.extract_install_candidates(text) == [
        ("pypi", "toolx"), ("npm", "@acme/toolx-cli"), ("npm", "create-thing"),
    ]
    assert sandbox.extract_install_candidates("pip install -e .") == []
    assert sandbox.extract_install_candidates("npx github:owner/repo") == []


def test_registry_must_point_to_the_same_repo() -> None:
    other = {**PYPI_TOOLX, "info": {**PYPI_TOOLX["info"], "project_urls": {"Source": "https://github.com/evil/x"}}}
    reg = registry({PYPI_JSON: PYPI_TOOLX, "https://pypi.org/pypi/other/json": other, **PYPROJECT})
    assert reg.resolve("pypi", "toolx", "acme/toolx") == sandbox.Resolved("pypi", "toolx", "1.2.0", [], "manifest")
    assert reg.resolve("pypi", "other", "acme/toolx") is None  # README упоминает чужой пакет — не запускаем
    assert reg.resolve("pypi", "missing", "acme/toolx") is None


def test_pypi_without_wheel_is_not_installed() -> None:
    sdist_only = {**PYPI_TOOLX, "urls": [{"packagetype": "sdist", "yanked": False}]}
    assert registry({PYPI_JSON: sdist_only, **PYPROJECT}).resolve("pypi", "toolx", "acme/toolx") is None


# --- происхождение: provenance или манифест репозитория -------------------------------------------------
def test_pypi_provenance_from_the_post_repo_is_enough() -> None:
    reg = registry({PYPI_JSON: PYPI_TOOLX, PYPI_PROV: pypi_provenance("Acme/ToolX")})  # манифеста нет
    assert reg.resolve("pypi", "toolx", "acme/toolx").origin == "provenance"


def test_pypi_provenance_from_another_repo_is_rejected_even_with_manifest() -> None:
    # сквоттер: пакет ссылается на репозиторий поста, но собран из своего
    reg = registry({PYPI_JSON: PYPI_TOOLX, PYPI_PROV: pypi_provenance("evil/toolx"), **PYPROJECT})
    assert reg.resolve("pypi", "toolx", "acme/toolx") is None
    gitlab = registry({PYPI_JSON: PYPI_TOOLX, PYPI_PROV: pypi_provenance("acme/toolx", kind="GitLab"), **PYPROJECT})
    assert gitlab.resolve("pypi", "toolx", "acme/toolx") is None


def test_pypi_without_provenance_needs_the_name_in_the_repo() -> None:
    assert registry({PYPI_JSON: PYPI_TOOLX}).resolve("pypi", "toolx", "acme/toolx") is None
    wrong = {RAW + "pyproject.toml": '[project]\nname = "something-else"\n'}
    assert registry({PYPI_JSON: PYPI_TOOLX, **wrong}).resolve("pypi", "toolx", "acme/toolx") is None
    poetry = {RAW + "pyproject.toml": '[tool.poetry]\nname = "ToolX"\n'}  # имена сравниваются нормализованными
    assert registry({PYPI_JSON: PYPI_TOOLX, **poetry}).resolve("pypi", "toolx", "acme/toolx").origin == "manifest"
    cfg = {RAW + "pyproject.toml": "[build-system]\nrequires = []\n", RAW + "setup.cfg": "[metadata]\nname = toolx\n"}
    assert registry({PYPI_JSON: PYPI_TOOLX, **cfg}).resolve("pypi", "toolx", "acme/toolx").origin == "manifest"
    broken = {RAW + "pyproject.toml": "[project\nname="}
    assert registry({PYPI_JSON: PYPI_TOOLX, **broken}).resolve("pypi", "toolx", "acme/toolx") is None


NPM_CLI = "https://registry.npmjs.org/@acme%2Ftoolx-cli/latest"
NPM_ATT = "https://registry.npmjs.org/-/npm/v1/attestations/@acme%2ftoolx-cli@2.0.1"
NPM_META = {"version": "2.0.1", "repository": {"url": "git+https://github.com/acme/toolx.git"},
            "bin": {"toolx": "bin/cli.js", "bad name;rm": "x"}}


def test_npm_bins_and_repo_formats() -> None:
    pkg = {RAW + "package.json": json.dumps({"name": "@acme/toolx-cli"})}
    reg = registry({
        NPM_CLI: NPM_META, **pkg,
        "https://registry.npmjs.org/libonly/latest": {"version": "1.0.0", "repository": "github:acme/toolx"},
    })
    res = reg.resolve("npm", "@acme/toolx-cli", "acme/toolx")
    assert res.bins == ["toolx"] and res.origin == "manifest"
    assert reg.resolve("npm", "libonly", "acme/toolx") is None  # библиотека: запускать нечего
    single = registry({"https://registry.npmjs.org/single/latest": {
        "version": "1.0.0", "repository": "git@github.com:acme/toolx.git", "bin": "cli.js"},
        RAW + "package.json": json.dumps({"name": "single"})})
    assert single.resolve("npm", "single", "acme/toolx").bins == ["single"]
    # package.json репозитория объявляет другое имя — без provenance не запускаем
    assert registry({NPM_CLI: NPM_META, RAW + "package.json": '{"name": "other"}'}).resolve(
        "npm", "@acme/toolx-cli", "acme/toolx") is None


@pytest.mark.parametrize(("source", "v1", "origin"), [
    ("https://github.com/acme/toolx", True, "provenance"),
    ("git+https://github.com/acme/toolx@refs/heads/main", False, "provenance"),
    ("https://github.com/evil/toolx", True, None),
])
def test_npm_provenance(source, v1, origin) -> None:
    meta = {**NPM_META, "dist": {"attestations": {"url": NPM_ATT, "provenance": {}}}}
    pkg = {RAW + "package.json": json.dumps({"name": "@acme/toolx-cli"})}  # при provenance манифест не решает
    res = registry({NPM_CLI: meta, NPM_ATT: npm_attestations(source, v1=v1), **pkg}).resolve(
        "npm", "@acme/toolx-cli", "acme/toolx")
    assert (res.origin if res else None) == origin


def test_private_manifest_does_not_vouch_for_a_registry_package() -> None:
    # владелец не публикует пакет (private / «Private :: Do Not Upload») — чужой пакет с этим именем не принимаем
    npm_private = {RAW + "package.json": json.dumps({"name": "@acme/toolx-cli", "private": True})}
    assert registry({NPM_CLI: NPM_META, **npm_private}).resolve("npm", "@acme/toolx-cli", "acme/toolx") is None
    py_private = {RAW + "pyproject.toml": '[project]\nname = "toolx"\nclassifiers = ["Private :: Do Not Upload"]\n'}
    assert registry({PYPI_JSON: PYPI_TOOLX, **py_private}).resolve("pypi", "toolx", "acme/toolx") is None


def test_require_provenance_setting(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, registry({PYPI_JSON: PYPI_TOOLX, **PYPROJECT}))
    cfg.sandbox.require_provenance = True
    assert sandbox.plan(rt, candidate_id="c1", url="https://github.com/acme/toolx", rubric="tool",
                        doc_text="pip install toolx") is None  # только манифест — не хватает
    rt.registry = registry({PYPI_JSON: PYPI_TOOLX, PYPI_PROV: pypi_provenance("acme/toolx")})
    assert sandbox.plan(rt, candidate_id="c1", url="https://github.com/acme/toolx", rubric="tool",
                        doc_text="pip install toolx") == "pypi:toolx"


def test_registry_redirects_only_within_the_same_host() -> None:
    reg = registry({"https://pypi.org/pypi/ToolX/json": ("redirect", PYPI_JSON), PYPI_JSON: PYPI_TOOLX, **PYPROJECT})
    assert reg.resolve("pypi", "ToolX", "acme/toolx").package == "toolx"
    evil = registry({"https://pypi.org/pypi/toolx/json": ("redirect", "https://evil.example/toolx.json")})
    with pytest.raises(ValueError):
        evil.resolve("pypi", "toolx", "acme/toolx")


def test_plan_creates_request_only_for_github_tools(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, registry({PYPI_JSON: PYPI_TOOLX, **PYPROJECT}))
    doc = "Quick start: pip install toolx"
    assert sandbox.plan(rt, candidate_id="c1", url="https://github.com/acme/toolx", rubric="tool",
                        doc_text=doc) == "pypi:toolx"
    row = rt.state.sandbox_row("c1")
    assert row["status"] == "pending" and row["version"] == "1.2.0" and row["origin"] == "manifest"
    assert sandbox.plan(rt, candidate_id="c2", url="https://acme.dev/blog", rubric="tool", doc_text=doc) is None
    assert sandbox.plan(rt, candidate_id="c3", url="https://github.com/acme/toolx", rubric="case",
                        doc_text=doc) is None


def test_manual_request_id_is_safe_for_scoped_npm() -> None:
    reg = registry({NPM_CLI: NPM_META})
    [req] = sandbox.manual_request(reg, "npm:@acme/toolx-cli")
    assert req["id"] == "test-npm-_acme_toolx-cli" and sandbox_runner.valid(req)


# --- публикация ---------------------------------------------------------------------------
def queued(rt, now) -> str:
    return rt.board.add_post(PostRecord(title="Tool X", rubric="tool", status=Status.APPROVED,
                                        source_url="https://github.com/acme/toolx", source_domain="github.com",
                                        score=12, found_at=now - timedelta(hours=1), html=HTML, candidate_id="c1"))


def request(rt, at, version="1.2.0") -> None:
    rt.state.add_sandbox_request(candidate_id="c1", repo="acme/toolx", ecosystem="pypi", package="toolx",
                                 version=version, bins=[], origin="manifest", now=at)


def test_publish_waits_for_sandbox_then_gives_up(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    queued(rt, now)
    request(rt, now - timedelta(minutes=30))
    s = run_publish(rt)
    assert s["status"] == "nothing_to_publish" and s["sandbox_waiting"] == 1
    rt.state.db.execute("UPDATE sandbox SET requested_at=?", ((now - timedelta(minutes=121)).isoformat(),))
    rt._settings = None
    assert run_publish(rt)["status"] == "published"
    assert "Запущено" not in rt.tg.sent[0][1]  # не дождались — без пометки
    # пост вышел — заявку больше не запускаем
    assert rt.state.sandbox_row("c1")["status"] == "expired"
    assert sandbox.export_requests(rt.state, 10, now=now, max_age_hours=24, max_attempts=2) == []


def test_successful_run_adds_mark_by_code(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    ref = queued(rt, now)
    request(rt, now - timedelta(minutes=30))
    rt.state.finish_sandbox("c1", "1.2.0", ok=True, stage="run", command="toolx --help", detail="usage", now=now)
    assert run_publish(rt)["status"] == "published"
    text = rt.tg.sent[0][1]
    lines = text.split("\n")
    assert lines[-4] == "🧪 Запущено в песочнице: установка и <code>toolx --help</code>"
    assert lines[-3] == 'Сверено с <a href="https://github.com/acme/toolx">первоисточником</a> ✅'
    assert lines[-2:] == ["", "#инструмент"]
    assert rt.board.get(ref).html == text  # в Notion — то, что ушло в канал
    assert rt.state.sandbox_row("c1")["status"] == "ok"  # результат не затирается


def test_mark_follows_the_latest_version(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    queued(rt, now)
    request(rt, now - timedelta(days=3), version="1.1.0")
    rt.state.finish_sandbox("c1", "1.1.0", ok=True, stage="run", command="toolx --help", detail="", now=now)
    request(rt, now - timedelta(minutes=10), version="1.2.0")  # новая версия — новая заявка, ждём её
    assert run_publish(rt)["status"] == "nothing_to_publish"


def test_failed_run_publishes_without_mark(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    queued(rt, now)
    request(rt, now - timedelta(minutes=30))
    rt.state.finish_sandbox("c1", "1.2.0", ok=False, stage="install", command="", detail="no wheel", now=now)
    assert run_publish(rt)["status"] == "published"
    assert "Запущено" not in rt.tg.sent[0][1]


# --- обмен с workflow ---------------------------------------------------------------------------
def test_export_skips_old_and_exhausted_requests(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    request(rt, now - timedelta(hours=1))
    rt.state.add_sandbox_request(candidate_id="old", repo="a/b", ecosystem="pypi", package="b", version="1",
                                 bins=[], now=now - timedelta(hours=30))
    rt.state.add_sandbox_request(candidate_id="tired", repo="a/c", ecosystem="pypi", package="c", version="1",
                                 bins=[], now=now - timedelta(hours=1))
    rt.state.sandbox_attempt("tired", "1")
    rt.state.sandbox_attempt("tired", "1")
    out = sandbox.export_requests(rt.state, 10, now=now, max_age_hours=24, max_attempts=2)
    assert out == [{"id": "c1", "ecosystem": "pypi", "package": "toolx", "version": "1.2.0", "bins": []}]


def write(path: Path, data) -> Path:
    path.write_text(json.dumps(data))
    return path


ASKED = [{"id": "c1", "ecosystem": "pypi", "package": "toolx", "version": "1.2.0", "bins": []}]


def test_apply_results_accepts_only_strict_answers(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    request(rt, now)
    rt.state.add_sandbox_request(candidate_id="c2", repo="a/b", ecosystem="pypi", package="b", version="1",
                                 bins=[], now=now)  # ожидает, но не в заявках этого запуска
    asked = write(tmp_path / "requests.json", ASKED)
    res = write(tmp_path / "results.json", [
        {"id": "unknown", "version": "1", "ok": True, "stage": "run", "command": "x --help"},
        {"id": "c2", "version": "1", "ok": True, "stage": "run", "command": "b --help"},
        {"id": ["c1"], "version": "1.2.0", "ok": True},
        {"id": "c1", "version": "9.9.9", "ok": True, "stage": "run", "command": "toolx --help"},
        {"id": "c1", "version": "1.2.0", "ok": True, "stage": "run", "command": "toolx --help; curl evil",
         "detail": "a\x00b"},
        {"id": "c1", "version": "1.2.0", "ok": True, "stage": "run", "command": "toolx --help"},  # повтор
    ])
    assert sandbox.apply_results(rt.state, res, asked, now, max_attempts=2) == {
        "ok": 0, "failed": 1, "retry": 0, "ignored": 5}
    row = rt.state.sandbox_row("c1")
    assert row["status"] == "failed" and row["stage"] == "invalid" and row["command"] == ""
    assert rt.state.sandbox_row("c2")["status"] == "pending"
    assert sandbox.apply_results(rt.state, res, asked, now, max_attempts=2)["ignored"] == 6  # не перезаписывает


@pytest.mark.parametrize(("eco", "bins", "command", "ok"), [
    ("pypi", [], "toolx --help", True),
    ("pypi", [], "toolx-admin --version", True),
    ("pypi", [], "rm --help", False),          # команда не про этот пакет
    ("npm", ["toolx"], "toolx --help", True),
    ("npm", ["toolx"], "other --help", False),  # не из заявки
])
def test_mark_command_must_belong_to_the_package(cfg, tmp_path, now, eco, bins, command, ok) -> None:
    rt = make_rt(cfg, tmp_path, now)
    rt.state.add_sandbox_request(candidate_id="c1", repo="acme/toolx", ecosystem=eco, package="toolx",
                                 version="1.2.0", bins=bins, now=now)
    asked = write(tmp_path / "requests.json", [{**ASKED[0], "ecosystem": eco, "bins": bins}])
    res = write(tmp_path / "results.json", [{"id": "c1", "version": "1.2.0", "ok": True, "stage": "run",
                                             "command": command}])
    sandbox.apply_results(rt.state, res, asked, now, max_attempts=2)
    assert rt.state.sandbox_row("c1")["status"] == ("ok" if ok else "failed")


def test_apply_survives_malformed_results(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    request(rt, now)
    asked = write(tmp_path / "requests.json", ASKED)
    res = write(tmp_path / "results.json", [
        {"id": "c1", "version": "1.2.0", "ok": True, "stage": ["run"], "command": {"x": 1}},
    ])
    assert sandbox.apply_results(rt.state, res, asked, now, max_attempts=2)["failed"] == 1
    rt.state.db.execute("UPDATE sandbox SET status='pending'")
    res = write(tmp_path / "results.json", [
        {"id": "c1", "version": "1.2.0", "ok": True, "stage": "run", "command": "toolx --help\n"}])
    sandbox.apply_results(rt.state, res, asked, now, max_attempts=2)
    assert rt.state.sandbox_row("c1")["command"] == ""  # перевод строки в <code> поста не попадёт


def test_lost_run_charges_the_first_request(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    request(rt, now - timedelta(minutes=5))
    rt.state.add_sandbox_request(candidate_id="c2", repo="a/b", ecosystem="pypi", package="b", version="1",
                                 bins=[], now=now)
    asked = write(tmp_path / "requests.json", [*ASKED, {"id": "c2", "version": "1"}])
    assert sandbox.charge_lost_run(rt.state, asked, now, max_attempts=2) == "c1"
    # у c1 попытка — следующий запуск начнёт с c2, а не снова с неё
    out = sandbox.export_requests(rt.state, 10, now=now, max_age_hours=24, max_attempts=2)
    assert [r["id"] for r in out] == ["c2", "c1"]
    sandbox.charge_lost_run(rt.state, asked, now, max_attempts=2)
    assert rt.state.sandbox_row("c1")["status"] == "failed"


def test_interrupted_request_is_retried_then_failed(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    request(rt, now)
    asked = write(tmp_path / "requests.json", ASKED)
    res = write(tmp_path / "results.json", [{"id": "c1", "version": "1.2.0", "ok": False, "stage": "started"}])
    assert sandbox.apply_results(rt.state, res, asked, now, max_attempts=2)["retry"] == 1
    assert rt.state.sandbox_row("c1")["status"] == "pending"
    assert sandbox.apply_results(rt.state, res, asked, now, max_attempts=2)["failed"] == 1
    row = rt.state.sandbox_row("c1")
    assert row["status"] == "failed" and row["stage"] == "error" and row["attempts"] == 2


def test_old_sandbox_table_is_migrated(tmp_path, now) -> None:
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE sandbox (candidate_id TEXT PRIMARY KEY, repo TEXT NOT NULL, ecosystem TEXT NOT NULL, "
               "package TEXT NOT NULL, version TEXT NOT NULL, bins TEXT NOT NULL, status TEXT NOT NULL, "
               "requested_at TEXT NOT NULL, finished_at TEXT, stage TEXT, command TEXT, detail TEXT)")
    db.execute("INSERT INTO sandbox VALUES ('c1','a/b','pypi','b','1','[]','ok','2026-10-07T00:00:00+00:00',"
               "NULL,'run','b --help','')")
    db.commit()
    db.close()
    state = State(tmp_path / "old.db")
    row = state.sandbox_row("c1")
    assert row["command"] == "b --help" and row["attempts"] == 0
    state.add_sandbox_request(candidate_id="c1", repo="a/b", ecosystem="pypi", package="b", version="2",
                              bins=[], now=now)  # та же находка, новая версия — отдельная заявка
    assert state.sandbox_row("c1")["version"] == "2"


# --- изоляция запуска ---------------------------------------------------------------------------
def has(argv: list[str], flag: list[str]) -> bool:
    return any(argv[i:i + len(flag)] == flag for i in range(len(argv)))


def test_runner_isolation_flags() -> None:
    run = sandbox_runner.docker_argv("n", "img", ["x", "--help"], workdir="/w", network=False, readonly=True)
    for flag in (["--network", "none"], ["--user", "1000:1000"], ["--cap-drop", "ALL"], ["--read-only"],
                 ["--security-opt", "no-new-privileges"], ["-v", "/w:/opt/pkg:ro"], ["--runtime", "runsc"],
                 ["--log-driver", "none"]):
        assert has(run, flag)
    assert "/var/run/docker.sock" not in " ".join(run) and "--privileged" not in run
    install = sandbox_runner.docker_argv("n", "img", ["pip"], workdir="/w", network=True, readonly=False)
    # с сетью — только внутренняя сеть, наружу через прокси
    assert has(install, ["--network", sandbox_runner.INT_NET]) and "bridge" not in install
    assert has(install, ["-e", f"HTTPS_PROXY={sandbox_runner.PROXY_URL}"]) and has(install, ["--runtime", "runsc"])


def test_package_code_never_runs_with_network() -> None:
    py = sandbox_runner.install_steps({"ecosystem": "pypi", "package": "toolx", "version": "1.2.0"})
    assert [net for _, _, net, _ in py] == [False, True]
    assert "--only-binary=:all:" in py[1][1]  # только wheel: setup.py не выполняется
    npm = sandbox_runner.install_steps({"ecosystem": "npm", "package": "x", "version": "1.0.0", "bins": ["x"]})
    image, argv, _, _ = npm[0]
    assert image.startswith("node:24-slim@sha256:")  # npm 11
    # без install-скриптов и без git/URL/файловых зависимостей (git-зависимость = вложенный npm install)
    for flag in ("--ignore-scripts", "--allow-git=none", "--allow-remote=none", "--allow-file=none",
                 "--allow-directory=none", "--git=/bin/false"):
        assert flag in argv


@pytest.mark.parametrize("req", [
    {"id": "a", "ecosystem": "pypi", "package": "x; rm -rf /", "version": "1"},
    {"id": "a", "ecosystem": "npm", "package": "x", "version": "1", "bins": ["../../bin/sh"]},
    {"id": "a", "ecosystem": "npm", "package": "x", "version": "1", "bins": []},
    {"id": "a", "ecosystem": "cargo", "package": "x", "version": "1"},
    {"id": "../x", "ecosystem": "pypi", "package": "x", "version": "1"},
    {"id": "test-npm-@acme/x", "ecosystem": "npm", "package": "@acme/x", "version": "1", "bins": ["x"]},
])
def test_runner_rejects_invalid_requests(req) -> None:
    assert not sandbox_runner.valid(req)


def test_pypi_scripts_read_as_text_without_symlinks(tmp_path) -> None:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "toolx-1.2.0.dist-info").mkdir(parents=True)
    (site / "toolx-1.2.0.dist-info" / "entry_points.txt").write_text(
        "[console_scripts]\nzz-other = toolx.b:main\ntoolx-admin = toolx.a:main\ntoolx = toolx.cli:main\n"
        "bad;name = x:y\n[gui_scripts]\ng = x:y\n")
    (site / "dep-1.0.dist-info").mkdir()
    (site / "dep-1.0.dist-info" / "entry_points.txt").write_text("[console_scripts]\ndep = d:m\n")
    # только команды «про пакет»: иначе job save не примет пометку
    assert sandbox_runner.pypi_scripts(tmp_path, "toolx") == ["toolx", "toolx-admin"]
    evil = site / "evil-1.0.dist-info"
    evil.mkdir()
    (evil / "entry_points.txt").symlink_to("/etc/hosts")
    assert sandbox_runner.pypi_scripts(tmp_path, "evil") == []


def test_pypi_binary_scripts_from_record(tmp_path) -> None:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "ruff-0.16.10.dist-info").mkdir(parents=True)
    (site / "ruff-0.16.10.dist-info" / "RECORD").write_text(
        "../../../bin/ruff,sha256=x,123\nruff/__init__.py,sha256=y,1\n../../../bin/../../evil,sha256=z,1\n")
    assert sandbox_runner.pypi_scripts(tmp_path, "ruff") == ["ruff"]


@pytest.mark.parametrize(("bin_name", "package", "ok"), [
    ("ruff", "ruff", True), ("http", "httpie", True), ("aider", "aider-chat", True), ("uvx", "uv", True),
    ("llm", "LLM", True), ("rm", "ruff", False), ("a", "aider-chat", False),
])
def test_related_command(bin_name, package, ok) -> None:
    assert sandbox_runner.related(bin_name, package) is ok


# --- прокси установки ---------------------------------------------------------------------------
@pytest.mark.parametrize(("head", "host"), [
    (b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443", "pypi.org"),
    (b"CONNECT Registry.NPMjs.org:443 HTTP/1.1", "registry.npmjs.org"),
    (b"CONNECT files.pythonhosted.org:443 HTTP/1.0\r\n", "files.pythonhosted.org"),
    (b"CONNECT evil.example:443 HTTP/1.1", None),
    (b"CONNECT pypi.org:22 HTTP/1.1", None),
    (b"CONNECT pypi.org.evil.example:443 HTTP/1.1", None),
    (b"GET http://pypi.org/simple/ HTTP/1.1", None),
    (b"CONNECT 169.254.169.254:443 HTTP/1.1", None),
])
def test_proxy_allows_only_registries(head, host) -> None:
    assert sandbox_runner.proxy_target(head) == host


def test_proxy_refuses_private_addresses(monkeypatch) -> None:
    def fake(addrs):
        return lambda *a, **k: [(2, 1, 6, "", (ip, 443)) for ip in addrs]

    monkeypatch.setattr(sandbox_runner.socket, "getaddrinfo", fake(["151.101.0.223"]))
    assert sandbox_runner.public_addrs("pypi.org") == ["151.101.0.223"]
    for bad in (["10.1.0.4"], ["151.101.0.223", "169.254.169.254"], ["127.0.0.1"], []):
        monkeypatch.setattr(sandbox_runner.socket, "getaddrinfo", fake(bad))
        assert sandbox_runner.public_addrs("pypi.org") == []


# --- надёжность запуска ---------------------------------------------------------------------------
def fake_docker(monkeypatch, code: str) -> None:
    """run() без Docker: вместо контейнера — python -c."""
    monkeypatch.setattr(sandbox_runner, "docker_argv", lambda *a, **k: [sys.executable, "-c", code])
    monkeypatch.setattr(sandbox_runner, "_kill", lambda name: None)


def test_run_limits_output(monkeypatch) -> None:
    fake_docker(monkeypatch, "import sys\nwhile True: sys.stdout.write('x' * 65536)")
    rc, out = sandbox_runner.run("img", [], workdir="/w", network=False, readonly=True, timeout=20)
    assert rc == 125 and "КБ" in out


def test_run_timeout_and_tail(monkeypatch) -> None:
    fake_docker(monkeypatch, "import time; print('start', flush=True); time.sleep(30)")
    assert sandbox_runner.run("img", [], workdir="/w", network=False, readonly=True, timeout=1)[0] == 124
    fake_docker(monkeypatch, "print('a' * 10000 + 'END')")
    rc, out = sandbox_runner.run("img", [], workdir="/w", network=False, readonly=True, timeout=20)
    assert rc == 0 and out.rstrip().endswith("END") and len(out) <= sandbox_runner.TAIL_BYTES


def stub_env(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_runner, "preflight", lambda: None)
    monkeypatch.setattr(sandbox_runner, "network_up", lambda script: None)
    monkeypatch.setattr(sandbox_runner, "network_down", lambda: None)
    monkeypatch.setattr(sandbox_runner, "isolation_check", lambda: None)
    monkeypatch.setattr(sandbox_runner, "npm_flags_ok", lambda: True)
    monkeypatch.setattr(sandbox_runner, "low_disk", lambda: None)


def reqs(n: int) -> list[dict]:
    return [{"id": f"c{i}", "ecosystem": "pypi", "package": f"p{i}", "version": "1"} for i in range(n)]


def test_runner_writes_results_after_each_request(monkeypatch, tmp_path) -> None:
    stub_env(monkeypatch)
    out = tmp_path / "results.json"
    seen_before: list[str] = []

    def fake_sandbox(req):
        seen_before.append(json.loads(out.read_text())[-1]["stage"])  # до запуска на диске — «started»
        if req["id"] == "c0":
            raise RuntimeError("boom")
        return {"id": req["id"], "version": "1", "ok": True, "stage": "run", "command": "p1 --help", "detail": ""}

    monkeypatch.setattr(sandbox_runner, "sandbox", fake_sandbox)
    assert sandbox_runner.main(["x", str(write(tmp_path / "r.json", reqs(2))), str(out)]) == 0
    assert seen_before == ["started", "started"]
    results = json.loads(out.read_text())
    assert [r["stage"] for r in results] == ["error", "run"]  # одна упавшая заявка не роняет остальные
    assert results[0]["id"] == "c0" and "RuntimeError" in results[0]["detail"]


def test_runner_stops_on_deadline_and_low_disk(monkeypatch, tmp_path) -> None:
    stub_env(monkeypatch)
    monkeypatch.setattr(sandbox_runner, "sandbox", lambda req: pytest.fail("не должно запускаться"))
    out = tmp_path / "results.json"
    monkeypatch.setattr(sandbox_runner, "DEADLINE", 0)
    assert sandbox_runner.main(["x", str(write(tmp_path / "r.json", reqs(2))), str(out)]) == 0
    assert json.loads(out.read_text()) == []
    monkeypatch.setattr(sandbox_runner, "DEADLINE", 3600)
    monkeypatch.setattr(sandbox_runner, "low_disk", lambda: "мало места")
    assert sandbox_runner.main(["x", str(tmp_path / "r.json"), str(out)]) == 0
    assert json.loads(out.read_text()) == []


def test_runner_refuses_without_gvisor(monkeypatch, tmp_path) -> None:
    stub_env(monkeypatch)
    monkeypatch.setattr(sandbox_runner, "preflight", lambda: "gVisor (runsc) не подключён к Docker")
    monkeypatch.setattr(sandbox_runner, "network_up", lambda script: pytest.fail("без gVisor — ничего"))
    out = tmp_path / "results.json"
    assert sandbox_runner.main(["x", str(write(tmp_path / "r.json", reqs(1))), str(out)]) == 3
    assert json.loads(out.read_text()) == []


def test_runner_refuses_when_isolation_is_broken(monkeypatch, tmp_path) -> None:
    stub_env(monkeypatch)
    monkeypatch.setattr(sandbox_runner, "isolation_check", lambda: "egress: 169.254.169.254:80 открыт")
    monkeypatch.setattr(sandbox_runner, "sandbox", lambda req: pytest.fail("изоляция нарушена — ничего"))
    out = tmp_path / "results.json"
    assert sandbox_runner.main(["x", str(write(tmp_path / "r.json", reqs(1))), str(out)]) == 4
    assert json.loads(out.read_text()) == []


def test_isolation_probe_checks_what_must_be_closed() -> None:
    probe = sandbox_runner.ISOLATION_PROBE
    compile(probe, "probe", "exec")
    for target in ("1.1.1.1", "169.254.169.254", "168.63.129.16", sandbox_runner.INT_GW, sandbox_runner.EGRESS_GW,
                   "example.com", sandbox_runner.PROXY_IP):
        assert target in probe


def test_npm_without_safety_flags_is_skipped_not_failed(monkeypatch, tmp_path) -> None:
    """Сбой окружения — не приговор пакету: npm-заявки остаются ожидающими, job падает (оповещение)."""
    stub_env(monkeypatch)
    monkeypatch.setattr(sandbox_runner, "npm_flags_ok", lambda: False)
    ran = []
    monkeypatch.setattr(sandbox_runner, "sandbox", lambda req: ran.append(req["id"]) or {
        "id": req["id"], "version": "1", "ok": True, "stage": "run", "command": "p0 --help", "detail": ""})
    npm = {"id": "n1", "ecosystem": "npm", "package": "x", "version": "1", "bins": ["x"]}
    out = tmp_path / "results.json"
    assert sandbox_runner.main(["x", str(write(tmp_path / "r.json", [npm, *reqs(1)])), str(out)]) == 5
    assert ran == ["c0"] and [r["id"] for r in json.loads(out.read_text())] == ["c0"]


def test_proxy_relay_keeps_long_downloads(monkeypatch) -> None:
    """Клиент молчит, а ответ идёт дольше PROXY_IDLE — туннель не обрывается; EOF доходит до клиента."""
    import socket
    import threading
    import time

    monkeypatch.setattr(sandbox_runner, "PROXY_IDLE", 1)
    client, proxy_client = socket.socketpair()
    proxy_upstream, server = socket.socketpair()
    t = threading.Thread(target=sandbox_runner.relay, args=(proxy_client, proxy_upstream), daemon=True)
    t.start()

    def serve() -> None:
        for _ in range(6):  # 6 × 0.4 с = 2.4 с > PROXY_IDLE
            server.sendall(b"x" * 1000)
            time.sleep(0.4)
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    got = b""
    client.settimeout(5)
    while chunk := client.recv(65536):
        got += chunk
    assert len(got) == 6000
    t.join(5)
    assert not t.is_alive()


def test_proxy_relay_stops_when_both_sides_are_silent(monkeypatch) -> None:
    import socket
    import time

    monkeypatch.setattr(sandbox_runner, "PROXY_IDLE", 0.3)
    _client, b = socket.socketpair()
    c, _server = socket.socketpair()
    started = time.monotonic()
    sandbox_runner.relay(b, c)
    assert time.monotonic() - started < 3


def test_preflight_requires_runsc(monkeypatch) -> None:
    calls = []

    def fake(*args, timeout=120):
        calls.append(args)
        out = '{"runc":{"path":"runc"}}' if args[0] == "info" else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(sandbox_runner, "_docker", fake)
    assert "gVisor" in sandbox_runner.preflight()
    assert [c[0] for c in calls] == ["info"]  # дальше не идём


# --- workflow ---------------------------------------------------------------------------
def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def test_workflow_run_job_has_no_secrets_and_no_permissions() -> None:
    wf = workflow("sandbox.yml")
    run = wf["jobs"]["run"]
    assert run["permissions"] == {}
    assert "secrets." not in json.dumps(run) and "checkout" not in json.dumps(run)
    plan = json.dumps(wf["jobs"]["plan"])
    assert "OPENAI" not in plan and "TELEGRAM" not in plan and "NOTION" not in plan
    assert wf["jobs"]["save"]["concurrency"]["group"] == "vibe-stack-state"
    assert wf["concurrency"]["group"] == "vibe-stack-sandbox"  # одна песочница за раз


def test_workflow_installs_pinned_gvisor_and_firewall() -> None:
    steps = json.dumps(workflow("sandbox.yml")["jobs"]["run"]["steps"], ensure_ascii=False)
    assert "GVISOR_SHA256" in steps and "sha256sum -c" in steps and "runsc install" in steps
    for rule in ("INPUT -i", "ESTABLISHED,RELATED", "169.254.0.0/16", "10.0.0.0/8", "sbx-egress", "sbx-int"):
        assert rule in steps
    assert sandbox_runner.INT_NET in steps and sandbox_runner.EGRESS_NET in steps


def test_save_job_token_only_for_state_steps() -> None:
    save = workflow("sandbox.yml")["jobs"]["save"]
    with_token = [s.get("run", "") for s in save["steps"] if "GITHUB_TOKEN" in json.dumps(s.get("env", {}))]
    assert with_token and all(r.startswith("scripts/state.sh") for r in with_token)
    apply = next(s for s in save["steps"] if "sandbox-apply" in s.get("run", ""))
    assert "--requests sandbox-in/requests.json" in apply["run"] and "env" not in apply
    assert any("sha256sum -c" in s.get("run", "") for s in save["steps"])  # заявки сверены с job plan
    # имена артефактов со случайной частью от job plan: job run не займёт их для следующей попытки
    names = [s["with"]["name"] for job in workflow("sandbox.yml")["jobs"].values() for s in job.get("steps", [])
             if "artifact" in s.get("uses", "")]
    assert len(names) == 5 and all(n.endswith(("needs.plan.outputs.nonce }}", "steps.export.outputs.nonce }}"))
                                   for n in names)


@pytest.mark.parametrize("name", ["_run.yml", "sandbox.yml", "ci.yml"])
def test_no_actions_cache_in_jobs_with_secrets_or_write_access(name) -> None:
    for job in workflow(name)["jobs"].values():
        for step in job.get("steps", []):
            if "setup-uv" in step.get("uses", ""):
                assert step["with"]["enable-cache"] is False
