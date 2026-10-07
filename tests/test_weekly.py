"""Итоги недели: числа считает код и передаёт в промпт D без пересчёта моделью."""

from __future__ import annotations

import json
from datetime import timedelta

from vibe_stack.board import LocalBoard
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.llm import FakeLLM
from vibe_stack.models import Candidate
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier
from vibe_stack.timeutil import local_date
from vibe_stack.weekly import compute_stats, run_weekly

WEEKLY = ('📋 <b>Итоги недели</b>\n\nПросмотрено 3, опубликовано 1.\n'
          '<a href="https://t.me/vibe_stack/10">Пост недели</a>')


def make_rt(cfg, tmp_path, now, responses) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    cfg.channel.username = "vibe_stack"
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "w", clock, "Europe/Moscow", responses),
                   fetcher=FixtureFetcher({}, clock), clock=clock, run_id="w", out_dir=tmp_path, mode="dry-run",
                   channel_id="@vibe_stack", sources_factory=lambda _: [])


def seed(rt: Runtime, now) -> None:
    day = local_date(now, "Europe/Moscow")
    for i, (decision, reasons, stage) in enumerate([("rejected", ["ad"], "prefilter"),
                                                     ("rejected", ["low_score"], "gate"),
                                                     ("queued", [], "board")]):
        c = Candidate(source="s", source_type="rss", url=f"https://x{i}.dev", title=f"Item {i}")
        rt.decision("collect", c, stage, decision, reasons)
    rt.state.record_published(ref="p1", rubric="tool", urgent=False, title="Item 2", source_url="https://x2.dev",
                              domain="x2.dev", published_at=now - timedelta(days=1), day=day, slot="10:00",
                              tg_message_id=10, counts_regular=True)


def test_stats_are_computed_by_code(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, {})
    seed(rt, now)
    stats, pub, examples = compute_stats(rt)
    assert stats["просмотрено"] == 3 and stats["отклонено"] == 2 and stats["опубликовано"] == 1
    assert stats["отклонено_по_причинам"] == {"реклама или партнёрская ссылка": 1, "мало практической пользы": 1}
    assert pub == [{"заголовок": "Item 2", "ссылка": "https://t.me/vibe_stack/10"}]
    assert {e["причина"] for e in examples} == {"реклама или партнёрская ссылка", "мало практической пользы"}


def test_weekly_publishes_and_passes_exact_stats(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, {("weekly", "weekly"): WEEKLY})
    seed(rt, now)
    summary = run_weekly(rt)
    assert summary["status"] == "published"
    prompt = rt.llm.calls[0][2]
    assert json.dumps(summary["stats"], ensure_ascii=False) in prompt
    assert rt.tg.sent and rt.tg.sent[0][1] == WEEKLY


def test_weekly_rejects_changed_numbers(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, {("weekly", "weekly"): WEEKLY.replace("Просмотрено 3", "Просмотрено 300")})
    seed(rt, now)
    s = run_weekly(rt)
    assert s["status"] == "lint_error" and any(e.startswith("unverified_numbers:300") for e in s["errors"])
    assert rt.tg.sent == []


def test_weekly_only_once_per_week_in_publish_mode(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now, {("weekly", "weekly"): WEEKLY})
    rt.mode = "publish"
    seed(rt, now)
    assert run_weekly(rt)["status"] == "published"
    rt._settings = None
    assert run_weekly(rt)["status"] == "already_published_this_week"
    rt.force = True
    rt._settings = None
    assert run_weekly(rt)["status"] == "published"
