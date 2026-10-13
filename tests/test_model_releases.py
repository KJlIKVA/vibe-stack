"""Решение 65: новые модели — сразу и подробно. Release notes Claude, анонс к записи changelog, один пост на модель,
у срочных нет дневного лимита — только предохранитель от сбоя."""

from __future__ import annotations

import copy
import json

import httpx

from vibe_stack.fetch import HttpFetcher
from vibe_stack.fixtures import load_fixtures, run_scenario
from vibe_stack.sources import build_source
from vibe_stack.sources.md_changelog import parse_entries
from vibe_stack.storage import State
from vibe_stack.urgent import model_key

from .conftest import FIXTURES, NOW

NOTES = """---
title: Claude Platform release notes
---

<Tip>
  For updates to Claude Code, see the CHANGELOG.
</Tip>

### October 7, 2026

* We've lowered the price of prompt cache reads on Claude Sonnet 5.5 from $0.20 USD to $0.10 USD per million tokens.
* We've launched **Claude Haiku 5.5** (`claude-haiku-5-5`), our most capable model tuned for high-volume work.
  It has a [1M token context window](https://platform.claude.com/docs/en/build-with-claude/context-windows).
  See [What's new in Claude Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/whats-new-haiku-5-5).

### October 6, 2026

* The Compliance API can now export chats.
"""

ANNOUNCE = """<html><head><title>Introducing Claude Haiku 5.5 \\ Anthropic</title>
<meta property="og:image" content="https://www.anthropic.com/og/haiku.png"></head><body><article>
<h1>Introducing Claude Haiku 5.5</h1><p>Terminal-Bench 4.0: Haiku 5.5 39.2%, Haiku 4.5 16.4%.</p>
<p>Price per 1 million tokens: input $0.50, output $2.50.</p></article></body></html>"""
WHATS_NEW = "# What's new in Claude Haiku 5.5\n\nBuilt for classification, routing and subagent tasks."
PAGE = "https://platform.claude.com/docs/en/release-notes/overview"


def client(routes: dict[str, tuple[str, str]], seen: list[str]) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if str(req.url) in routes:
            ctype, body = routes[str(req.url)]
            return httpx.Response(200, text=body, headers={"content-type": ctype})
        return httpx.Response(404, text="")
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_claude_release_notes_one_entry_per_item(cfg) -> None:
    entries = parse_entries(NOTES)
    assert [(e.day.date().isoformat(), e.title[:31]) for e in entries] == [
        ("2026-10-07", "We've lowered the price of prom"),
        ("2026-10-07", "We've launched Claude Haiku 5.5"),
        ("2026-10-06", "The Compliance API can now expo"),
    ]
    assert "What's new in Claude Haiku 5.5" in entries[1].body  # строки-продолжения пункта — в той же записи
    src = next(s for s in cfg.sources if s.name == "claude-release-notes")
    assert src.whitelist and src.model_vendor and "urgent" in src.contours
    got = build_source(src, client({PAGE + ".md": ("text/markdown", NOTES)}, []), lambda: NOW, 30).collect()
    assert got[1].title.startswith("Claude API: We've launched Claude Haiku 5.5")
    assert got[1].url.startswith(PAGE + "?entry=2026-10-07-") and got[1].url != got[0].url


def test_launch_entry_reads_the_announcement(cfg) -> None:
    seen: list[str] = []
    routes = {PAGE + ".md": ("text/markdown", NOTES),
              "https://www.anthropic.com/claude-haiku-5-5": ("text/html", ANNOUNCE),
              "https://platform.claude.com/docs/en/models/haiku-5-5/whats-new-haiku-5-5.md": ("text/markdown",
                                                                                              WHATS_NEW)}
    src = next(s for s in cfg.sources if s.name == "claude-release-notes")
    price, launch, _ = build_source(src, client(routes, []), lambda: NOW, 30).collect()
    fetcher = HttpFetcher(cfg.fetch, lambda: NOW, client(routes, seen), check_urls=False)
    doc = fetcher.fetch(launch.url, purpose="score")
    assert doc.ok and "Terminal-Bench 4.0: Haiku 5.5 39.2%" in doc.text and "$2.50" in doc.text
    assert "Built for classification, routing" in doc.text and doc.image == "https://www.anthropic.com/og/haiku.png"
    assert doc.text.index("We've launched") < doc.text.index("Terminal-Bench") < doc.text.index("Built for")
    assert "https://platform.claude.com/docs/en/build-with-claude/context-windows.md" not in seen  # не /models/
    seen.clear()
    assert "Terminal-Bench" not in fetcher.fetch(price.url, purpose="score").text  # не выход модели — без анонса
    assert seen == [PAGE + ".md"]


def test_model_key_matches_names_and_ids() -> None:
    assert model_key("Claude Haiku 5.5") == model_key("claude-haiku-5-5") == model_key("Haiku 5.5") == "haiku 5.5"
    assert model_key("GPT-6.1 Sol") == model_key("gpt-6.1-sol") == "gpt 6.1 sol"
    assert model_key("Claude Opus 5") != model_key("Claude Opus 5.5")


BASE = next(f for f in load_fixtures(FIXTURES, NOW) if f.id == "10")


def model_fx(source: str = "openai-news"):
    fx = copy.deepcopy(BASE)
    fx.expected = {}
    c = fx.candidates[0]
    fx.candidates = [c.model_copy(update={"source": source})]
    triage = next(k for k in fx.llm if k[0] == "triage")
    fx.llm[triage] = {**fx.llm[triage], "model": "FixtureModel-1"}
    return fx


def remember(tmp_path, vendor: bool) -> State:
    state = State(tmp_path / "state.db")
    state.put("model_post:fixturemodel 1", json.dumps({"day": "2026-10-07", "vendor": vendor}))
    return state


def test_new_model_post_is_remembered(cfg, tmp_path) -> None:
    res = run_scenario(model_fx(), cfg, NOW, workdir=tmp_path)
    assert len(res.sent) == 1
    assert json.loads(State(tmp_path / "state.db").get("model_post:fixturemodel 1"))["vendor"] is True


def test_second_post_about_the_same_model_is_not_urgent(cfg, tmp_path) -> None:
    remember(tmp_path, vendor=True)
    res = run_scenario(model_fx(), cfg, NOW, workdir=tmp_path)
    assert res.sent == [] and res.actual["decisions"][-1]["reasons"] == '["model_already_posted:fixturemodel 1"]'
    assert [step for step, *_ in res.llm_calls] == ["triage"]  # проверку и текст не тратим


def test_third_party_post_after_vendor_is_not_urgent(cfg, tmp_path) -> None:
    remember(tmp_path, vendor=True)
    assert run_scenario(model_fx("github-changelog"), cfg, NOW, workdir=tmp_path).sent == []


def test_vendor_post_goes_out_after_third_party(cfg, tmp_path) -> None:
    """Сначала «модель в GitHub Copilot», потом анонс самой компании: анонс всё равно выходит — в нём цена и
    бенчмарки."""
    state = remember(tmp_path, vendor=False)
    assert len(run_scenario(model_fx(), cfg, NOW, workdir=tmp_path).sent) == 1
    assert json.loads(state.get("model_post:fixturemodel 1"))["vendor"] is True


def test_urgent_fuse_after_20_a_day(cfg, tmp_path) -> None:
    """У срочных нет дневного лимита, но 20 за день — похоже на сбой: дальше в очередь обычных и оповещение."""
    fx = model_fx()
    fx.preseed = {"urgent_published_today": cfg.limits.urgent_per_day}
    res = run_scenario(fx, cfg, NOW, workdir=tmp_path)
    assert res.sent == [] and res.actual["decisions"][-1]["decision"] == "queued_regular"
    assert "предохранитель" in (tmp_path / "admin.log").read_text(encoding="utf-8")
    assert cfg.limits.urgent_per_day == 20
