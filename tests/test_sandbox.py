"""Песочница: выбор пакета, проверка реестра, ожидание и пометка при публикации, приём результатов, изоляция."""

from __future__ import annotations

import json
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
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                   clock=clock, run_id="p", out_dir=tmp_path, mode="dry-run", channel_id="@c",
                   sources_factory=lambda _: [], registry=registry)


def registry(routes: dict[str, dict]) -> sandbox.Registry:
    def handler(req: httpx.Request) -> httpx.Response:
        key = str(req.url)
        return httpx.Response(200, json=routes[key]) if key in routes else httpx.Response(404, json={})

    return sandbox.Registry(httpx.Client(transport=httpx.MockTransport(handler)))


PYPI_TOOLX = {"info": {"name": "toolx", "version": "1.2.0", "home_page": "",
                       "project_urls": {"Source": "https://github.com/acme/toolx"}},
              "urls": [{"packagetype": "bdist_wheel", "yanked": False}]}


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
    reg = registry({"https://pypi.org/pypi/toolx/json": PYPI_TOOLX, "https://pypi.org/pypi/other/json": other})
    assert reg.resolve("pypi", "toolx", "acme/toolx") == sandbox.Resolved("pypi", "toolx", "1.2.0", [])
    assert reg.resolve("pypi", "other", "acme/toolx") is None  # README упоминает чужой пакет — не запускаем
    assert reg.resolve("pypi", "missing", "acme/toolx") is None


def test_pypi_without_wheel_is_not_installed() -> None:
    sdist_only = {**PYPI_TOOLX, "urls": [{"packagetype": "sdist", "yanked": False}]}
    assert registry({"https://pypi.org/pypi/toolx/json": sdist_only}).resolve("pypi", "toolx", "acme/toolx") is None


def test_npm_bins_and_repo_formats() -> None:
    reg = registry({
        "https://registry.npmjs.org/@acme%2Ftoolx-cli/latest": {
            "version": "2.0.1", "repository": {"url": "git+https://github.com/acme/toolx.git"},
            "bin": {"toolx": "bin/cli.js", "bad name;rm": "x"}},
        "https://registry.npmjs.org/libonly/latest": {"version": "1.0.0", "repository": "github:acme/toolx"},
        "https://registry.npmjs.org/single/latest": {"version": "1.0.0", "repository": "git@github.com:acme/toolx.git",
                                                     "bin": "cli.js"},
    })
    assert reg.resolve("npm", "@acme/toolx-cli", "acme/toolx").bins == ["toolx"]
    assert reg.resolve("npm", "libonly", "acme/toolx") is None  # библиотека: запускать нечего
    assert reg.resolve("npm", "single", "acme/toolx").bins == ["single"]


def test_plan_creates_request_only_for_github_tools(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, registry({"https://pypi.org/pypi/toolx/json": PYPI_TOOLX}))
    doc = "Quick start: pip install toolx"
    assert sandbox.plan(rt, candidate_id="c1", url="https://github.com/acme/toolx", rubric="tool",
                        doc_text=doc) == "pypi:toolx"
    assert rt.state.sandbox_row("c1")["status"] == "pending"
    assert sandbox.plan(rt, candidate_id="c2", url="https://acme.dev/blog", rubric="tool", doc_text=doc) is None
    assert sandbox.plan(rt, candidate_id="c3", url="https://github.com/acme/toolx", rubric="case",
                        doc_text=doc) is None


# --- публикация ---------------------------------------------------------------------------
def queued(rt, now) -> str:
    return rt.board.add_post(PostRecord(title="Tool X", rubric="tool", status=Status.APPROVED,
                                        source_url="https://github.com/acme/toolx", source_domain="github.com",
                                        score=12, found_at=now - timedelta(hours=1), html=HTML, candidate_id="c1"))


def request(rt, at) -> None:
    rt.state.add_sandbox_request(candidate_id="c1", repo="acme/toolx", ecosystem="pypi", package="toolx",
                                 version="1.2.0", bins=[], now=at)


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


def test_successful_run_adds_mark_by_code(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    ref = queued(rt, now)
    request(rt, now - timedelta(minutes=30))
    rt.state.finish_sandbox("c1", ok=True, stage="run", command="toolx --help", detail="usage", now=now)
    assert run_publish(rt)["status"] == "published"
    text = rt.tg.sent[0][1]
    lines = text.split("\n")
    assert lines[-2] == "🧪 Запущено в песочнице: установка и <code>toolx --help</code>"
    assert lines[-1].startswith("✅ Сверено")
    assert rt.board.get(ref).html == text  # в Notion — то, что ушло в канал


def test_failed_run_publishes_without_mark(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    queued(rt, now)
    request(rt, now - timedelta(minutes=30))
    rt.state.finish_sandbox("c1", ok=False, stage="install", command="", detail="no wheel", now=now)
    assert run_publish(rt)["status"] == "published"
    assert "Запущено" not in rt.tg.sent[0][1]


# --- приём результатов ---------------------------------------------------------------------------
def test_apply_results_accepts_only_strict_answers(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    request(rt, now)
    path = tmp_path / "results.json"
    path.write_text(json.dumps([
        {"id": "unknown", "ok": True, "stage": "run", "command": "x --help"},
        {"id": "c1", "ok": True, "stage": "run", "command": "toolx --help; curl evil", "detail": "a\x00b"},
    ]))
    assert sandbox.apply_results(rt.state, path, now) == {"ok": 0, "failed": 1, "ignored": 1}
    row = rt.state.sandbox_row("c1")
    assert row["status"] == "failed" and row["stage"] == "invalid" and row["command"] == ""
    assert sandbox.apply_results(rt.state, path, now)["ignored"] == 2  # повтор не перезаписывает


# --- изоляция запуска ---------------------------------------------------------------------------
def test_runner_isolation_flags() -> None:
    run = sandbox_runner.docker_argv("n", "img", ["x", "--help"], workdir="/w", network=False, readonly=True)
    for flag in (["--network", "none"], ["--user", "1000:1000"], ["--cap-drop", "ALL"], ["--read-only"],
                 ["--security-opt", "no-new-privileges"], ["-v", "/w:/opt/pkg:ro"]):
        assert any(run[i:i + len(flag)] == flag for i in range(len(run)))
    assert "/var/run/docker.sock" not in " ".join(run) and "--privileged" not in run


def test_package_code_never_runs_with_network() -> None:
    py = sandbox_runner.install_steps({"ecosystem": "pypi", "package": "toolx", "version": "1.2.0"})
    assert [net for _, _, net in py] == [False, True]
    assert "--only-binary=:all:" in py[1][1]  # только wheel: setup.py не выполняется
    npm = sandbox_runner.install_steps({"ecosystem": "npm", "package": "x", "version": "1.0.0", "bins": ["x"]})
    assert "--ignore-scripts" in npm[0][1]  # без install-скриптов


@pytest.mark.parametrize("req", [
    {"id": "a", "ecosystem": "pypi", "package": "x; rm -rf /", "version": "1"},
    {"id": "a", "ecosystem": "npm", "package": "x", "version": "1", "bins": ["../../bin/sh"]},
    {"id": "a", "ecosystem": "npm", "package": "x", "version": "1", "bins": []},
    {"id": "a", "ecosystem": "cargo", "package": "x", "version": "1"},
    {"id": "../x", "ecosystem": "pypi", "package": "x", "version": "1"},
])
def test_runner_rejects_invalid_requests(req) -> None:
    assert not sandbox_runner.valid(req)


def test_pypi_scripts_read_as_text_without_symlinks(tmp_path) -> None:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "toolx-1.2.0.dist-info").mkdir(parents=True)
    (site / "toolx-1.2.0.dist-info" / "entry_points.txt").write_text(
        "[console_scripts]\nzz-other = toolx.b:main\ntoolx = toolx.cli:main\nbad;name = x:y\n[gui_scripts]\ng = x:y\n")
    (site / "dep-1.0.dist-info").mkdir()
    (site / "dep-1.0.dist-info" / "entry_points.txt").write_text("[console_scripts]\ndep = d:m\n")
    assert sandbox_runner.pypi_scripts(tmp_path, "toolx") == ["toolx", "zz-other"]
    evil = site / "evil-1.0.dist-info"
    evil.mkdir()
    (evil / "entry_points.txt").symlink_to("/etc/hosts")
    assert sandbox_runner.pypi_scripts(tmp_path, "evil") == []


def test_workflow_run_job_has_no_secrets_and_no_permissions() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/sandbox.yml").read_text(encoding="utf-8"))
    run = wf["jobs"]["run"]
    assert run["permissions"] == {}
    assert "secrets." not in json.dumps(run) and "checkout" not in json.dumps(run)
    plan = json.dumps(wf["jobs"]["plan"])
    assert "OPENAI" not in plan and "TELEGRAM" not in plan and "NOTION" not in plan
    assert wf["jobs"]["save"]["concurrency"]["group"] == "vibe-stack-state"


def test_pypi_binary_scripts_from_record(tmp_path) -> None:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "ruff-0.16.10.dist-info").mkdir(parents=True)
    (site / "ruff-0.16.10.dist-info" / "RECORD").write_text(
        "../../../bin/ruff,sha256=x,123\nruff/__init__.py,sha256=y,1\n../../../bin/../../evil,sha256=z,1\n")
    assert sandbox_runner.pypi_scripts(tmp_path, "ruff") == ["ruff"]
