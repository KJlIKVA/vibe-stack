"""Закреп: создаётся и закрепляется один раз, правится только при изменении, хранит прошлую дату при сбое."""

from __future__ import annotations

from vibe_stack.board import LocalBoard
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.fixtures import FixtureLeaderboard
from vibe_stack.llm import FakeLLM
from vibe_stack.pin import run_pin
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier, TelegramError


def make_rt(cfg, tmp_path, now) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                   clock=clock, run_id="p", out_dir=tmp_path, mode="dry-run", channel_id="@c",
                   sources_factory=lambda _: [])


def arena(top, date="2026-10-06", fail=False):
    return FixtureLeaderboard({"key": "arena_text", "label": "Arena · текст", "date": date, "top": top,
                               "data_url": "https://example.org/arena", "fail": fail})


def test_create_pin_once_then_edit_only_on_change(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    s = run_pin(rt, [arena(["A", "B", "C"])])
    assert s["status"] == "created_and_pinned" and rt.tg.pinned == [-1]
    assert "1. A  2. B  3. C" in rt.tg.sent[0][1] and "данные на 2026-10-06" in rt.tg.sent[0][1]
    assert run_pin(rt, [arena(["A", "B", "C"])])["status"] == "unchanged"
    s = run_pin(rt, [arena(["B", "A", "C"], date="2026-10-07")])
    assert s["status"] == "edited" and len(rt.tg.sent) == 1 and len(rt.tg.pinned) == 1
    assert "1. B  2. A" in rt.tg.edited[-1][1]


def test_failure_keeps_previous_date(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [arena(["A", "B", "C"], date="2026-10-05")])
    s = run_pin(rt, [arena([], fail=True)])
    assert s["status"] == "unchanged" and s["problems"]
    # словарь изменился — закреп правится, но дата рейтинга остаётся прошлой, «свежую» не выдумываем
    rt.state.add_glossary(term="MCP", source_url="https://x", definition="d", published_at=now, post_url=None)
    s = run_pin(rt, [arena([], fail=True)])
    assert s["status"] == "edited"
    text = rt.tg.edited[-1][1]
    assert "данные на 2026-10-05" in text and "MCP" in text


def test_no_permitted_source_means_no_rating_block(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [])
    text = rt.tg.sent[0][1]
    assert "Топ моделей" not in text and "#инструмент" in text


def test_deleted_pin_is_recreated(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    run_pin(rt, [arena(["A", "B", "C"])])

    def gone(*a, **k):
        raise TelegramError("editMessageText: 400 Bad Request: message to edit not found", retryable=False)

    rt.tg.edit_message_text = gone
    s = run_pin(rt, [arena(["X", "Y", "Z"], date="2026-10-07")])
    assert s["status"] == "created_and_pinned" and len(rt.tg.pinned) == 2


def test_pause_blocks_pin(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    rt.board.set_settings(pause=True)
    assert run_pin(rt, [arena(["A", "B", "C"])])["status"] == "paused"
    assert rt.tg.sent == []
