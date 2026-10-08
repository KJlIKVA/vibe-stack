"""Срочный контур: режим approve, свежесть по дате публикации, сбои доски."""

from __future__ import annotations

import copy
from datetime import timedelta

from vibe_stack.board import BoardUnavailable, LocalBoard, RubricOverride
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.fixtures import load_fixtures, run_scenario
from vibe_stack.llm import FakeLLM
from vibe_stack.models import Status
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier
from vibe_stack.urgent import run_urgent, urgent_date

from .conftest import FIXTURES, NOW

BASE = next(f for f in load_fixtures(FIXTURES, NOW) if f.id == "10")


def variant(**changes):
    fx = copy.deepcopy(BASE)
    fx.expected = {}
    for k, v in changes.items():
        setattr(fx, k, v)
    return fx


def test_approve_mode_waits_then_publishes_next_tick(cfg, tmp_path) -> None:
    fx = variant(board={"rubrics": {"urgent": {"mode": "approve"}}})
    res = run_scenario(fx, cfg, NOW, workdir=tmp_path)
    assert res.actual["summary"]["pending_approval"] == 1 and res.sent == []
    board = LocalBoard(tmp_path / "board.json")
    (post,) = board.posts_with_status(Status.PENDING)
    assert post.urgent
    # человек одобрил → следующий тик срочного контура публикует, не дожидаясь слота
    board.update_post(post.ref, status=Status.APPROVED)
    board.data.rubrics["urgent"] = RubricOverride(mode="approve")
    state = State(tmp_path / "state.db")
    clock = lambda: NOW + timedelta(minutes=30)  # noqa: E731
    tg = DryRunTelegram(tmp_path / "tg2")
    rt = Runtime(cfg=cfg, state=state, board=board, tg=tg, notifier=Notifier(None, None, tmp_path / "a.log"),
                 llm=FakeLLM(cfg.llm, state, "t2", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                 clock=clock, run_id="t2", out_dir=tmp_path, mode="dry-run", channel_id="@c",
                 sources_factory=lambda _: [], fixture_candidates=fx.candidates)
    s = run_urgent(rt)
    assert s["published"] == 1 and len(tg.sent) == 1
    assert board.get(post.ref).status == Status.PUBLISHED


def test_feed_update_does_not_make_old_news_fresh(cfg, tmp_path) -> None:
    fx = variant()
    c = fx.candidates[0]
    fx.candidates = [c.model_copy(update={"published_at": NOW - timedelta(days=20), "updated_at": NOW})]
    assert urgent_date(fx.candidates[0]) == NOW - timedelta(days=20)
    res = run_scenario(fx, cfg, NOW, workdir=tmp_path)
    assert res.sent == [] and res.llm_calls == []


def test_page_date_overrides_fresh_feed_date(cfg, tmp_path) -> None:
    fx = variant()
    url = next(iter(fx.documents))
    fx.documents[url] = {**fx.documents[url], "published_meta": "2026-08-01T00:00:00Z"}
    res = run_scenario(fx, cfg, NOW, workdir=tmp_path)
    assert res.sent == [] and res.llm_calls == []
    assert res.actual["decisions"][-1]["stage"] == "freshness"


def test_board_write_failure_is_not_retried_every_tick(cfg, tmp_path) -> None:
    class NoAdd(LocalBoard):
        def add_post(self, post):
            raise BoardUnavailable("Notion: не удалось создать строку поста")

    import vibe_stack.fixtures as fx_mod

    orig = fx_mod.LocalBoard
    fx_mod.LocalBoard = NoAdd
    try:
        res = run_scenario(variant(), cfg, NOW, workdir=tmp_path)
    finally:
        fx_mod.LocalBoard = orig
    assert res.actual["summary"]["status"] == "board_unavailable" and res.sent == []
    state = State(tmp_path / "state.db")
    keys = ["u|" + k for k in BASE.candidates[0].keys]
    assert state.seen_outcome(keys, NOW + timedelta(minutes=30), 60) == "board_error"
    assert state.seen_outcome(keys, NOW + timedelta(hours=7), 60) is None  # потом можно попробовать снова


def test_new_model_post_gets_price_notes_longer_limit_and_chart(cfg, tmp_path) -> None:
    """Решение 47: в посте о новой модели — цена и сравнение (блок владельца, до 900 знаков) и график из статьи."""
    fx = copy.deepcopy(BASE)
    cid = fx.candidates[0].id
    doc = fx.documents[next(iter(fx.documents))]
    doc["image"] = "https://openai.com/hero.png"
    doc["figures"] = ["https://openai.com/team.png", "https://openai.com/evals.png"]
    fx.llm[("image", f"{cid}#image")] = {"index": 1, "kind": "benchmark"}
    res = run_scenario(fx, cfg, NOW, workdir=tmp_path)
    assert res.ok, res.mismatches
    write_prompt = next(p for step, _, p in res.llm_calls if step == "write")
    assert "<новая_модель>" in write_prompt and "<b>Цена:</b>" in write_prompt
    state = State(tmp_path / "state.db")
    assert state.get(f"image:{cid}") == "https://openai.com/evals.png"
    assert res.sent[0][1].rstrip().endswith("#срочно")
