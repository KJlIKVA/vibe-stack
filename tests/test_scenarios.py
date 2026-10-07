"""Раздел 12: dry-run на фикстурах даёт ожидаемые решения (ответы модели — из фикстур)."""

from __future__ import annotations

import pytest

from vibe_stack.fixtures import load_fixtures, run_scenario
from vibe_stack.storage import State

from .conftest import FIXTURES, NOW

PHASE1 = load_fixtures(FIXTURES, NOW, max_phase=2)


def test_all_phase1_fixtures_present() -> None:
    assert sorted(f.id for f in PHASE1) == ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12",
                                            "13", "14", "15", "16", "17", "18"]


@pytest.mark.parametrize("fx", PHASE1, ids=[f"{f.id}-{f.scenario}" for f in PHASE1])
def test_fixture_decision(fx, cfg, tmp_path) -> None:
    res = run_scenario(fx, cfg, NOW, workdir=tmp_path)
    assert res.ok, "\n".join(res.mismatches) + f"\n{res.actual['decisions']}"


def _get(fid: str):
    return next(f for f in PHASE1 if f.id == fid)


def test_injection_never_reaches_model(cfg, tmp_path) -> None:
    res = run_scenario(_get("03"), cfg, NOW, workdir=tmp_path)
    assert res.llm_calls == []  # код остановил до модели — фразу некому «выполнять»
    assert all("verified" not in p.reject_reason.lower() for p in res.board.data.posts)


def test_verify_runs_in_clean_context(cfg, tmp_path) -> None:
    res = run_scenario(_get("01"), cfg, NOW, workdir=tmp_path)
    steps = [s for s, _, _ in res.llm_calls]
    assert steps == ["score", "verify", "write"]
    verify_prompt = res.llm_calls[1][2]
    # в проверку B не попадают ни баллы, ни черновик поста
    assert "usefulness" not in verify_prompt and "novelty" not in verify_prompt
    assert "Первоисточник</a>" not in verify_prompt
    assert '"mode": "standard"' in verify_prompt


def test_verify_refetches_source(cfg, tmp_path) -> None:
    """Источник для B загружается заново (второй запрос), а не берётся из шага A."""
    from vibe_stack import fixtures as fx_mod

    seen: list[tuple[str, str]] = []
    orig = fx_mod.FixtureFetcher.fetch

    def spy(self, url, *, purpose):
        seen.append((url, purpose))
        return orig(self, url, purpose=purpose)

    fx_mod.FixtureFetcher.fetch = spy
    try:
        run_scenario(_get("01"), cfg, NOW, workdir=tmp_path)
    finally:
        fx_mod.FixtureFetcher.fetch = orig
    assert [p for _, p in seen] == ["score", "verify"]


def test_urgent_does_not_use_regular_limit(cfg, tmp_path) -> None:
    res = run_scenario(_get("10"), cfg, NOW, workdir=tmp_path)
    assert res.ok
    state = State(tmp_path / "state.db")
    from vibe_stack.timeutil import local_date

    day = local_date(NOW, cfg.channel.tz)
    assert state.count_published(day, urgent=True) == 1
    assert state.count_published(day, urgent=False) == 0


def test_pause_blocks_without_model_calls(cfg, tmp_path) -> None:
    res = run_scenario(_get("18"), cfg, NOW, workdir=tmp_path)
    assert res.ok
    assert res.llm_calls == []
    assert res.sent == []


def test_approve_flow_publishes_only_after_approval(cfg, tmp_path) -> None:
    res = run_scenario(_get("15"), cfg, NOW, workdir=tmp_path)
    assert res.ok, res.mismatches
    assert res.actual["publish_before"]["status"] == "nothing_to_publish"
    assert len(res.sent) == 1 and "Мнение ИИ" in res.sent[0][1]


def test_collect_runs_once_per_day_in_publish_mode(cfg, tmp_path) -> None:
    from vibe_stack.board import LocalBoard
    from vibe_stack.collect import run_collect
    from vibe_stack.fetch import FixtureFetcher
    from vibe_stack.llm import FakeLLM
    from vibe_stack.runtime import Runtime
    from vibe_stack.telegram import DryRunTelegram, Notifier

    fx = _get("01")
    state = State(tmp_path / "s.db")
    clock = lambda: NOW  # noqa: E731
    rt = Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                 notifier=Notifier(None, None, tmp_path / "a.log"),
                 llm=FakeLLM(cfg.llm, state, "c", clock, "Europe/Moscow", fx.llm),
                 fetcher=FixtureFetcher(fx.documents, clock), clock=clock, run_id="c", out_dir=tmp_path,
                 mode="publish", channel_id="@c", sources_factory=lambda _: [], fixture_candidates=fx.candidates)
    assert run_collect(rt)["queued"] == 1
    rt._settings = None
    assert run_collect(rt)["status"] == "already_ran_today"
