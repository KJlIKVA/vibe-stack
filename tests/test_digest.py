"""Сводка дня админу: числа считает код, отправляется раз в день, пауза её не останавливает."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vibe_stack.board import LocalBoard
from vibe_stack.digest import digest_day, run_digest
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.llm import FakeLLM
from vibe_stack.models import PostRecord, Status
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier

NIGHT = datetime(2026, 10, 7, 20, 45, tzinfo=UTC)  # 23:45 МСК


def make_rt(cfg, tmp_path, now=NIGHT, mode="publish") -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "d", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                   clock=clock, run_id="d", out_dir=tmp_path, mode=mode, channel_id="@c",
                   sources_factory=lambda _: [])


def test_digest_counts_and_sends_once(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path)
    for i, rubric in enumerate(["tool", "tool", "trick"]):
        rt.state.record_published(ref=f"r{i}", rubric=rubric, urgent=False, title=f"P{i}",
                                  source_url=f"https://x{i}.dev", domain=f"x{i}.dev",
                                  published_at=NIGHT - timedelta(hours=3 + i), day=digest_day(rt), slot="10:00",
                                  tg_message_id=i + 1, counts_regular=True)
    rt.board.add_post(PostRecord(title="Разбор статьи про агентов", rubric="analysis", status=Status.PENDING, html="x"))
    rt.board.add_post(PostRecord(title="Q", rubric="tool", status=Status.APPROVED, html="x"))
    rt.board.set_settings(pause=True)
    rt.state.record_llm_call(run_id="x", step="score", model="gpt-5.6-terra", now=NIGHT - timedelta(hours=1),
                             day=digest_day(rt), input_tokens=400_000, cached_tokens=0, output_tokens=100_000,
                             cost_usd=1.0, ok=True)
    assert run_digest(rt)["status"] == "sent"
    text = rt.notifier.sent[-1]
    assert "Сводка за 07.10" in text and "Пауза" in text
    assert "Вышло: 3 из 32" in text and "🛠 Инструмент — 2, 💡 Приём — 1" in text
    assert "Ждут вашего одобрения в Notion: 1" in text and "Разбор статьи про агентов" in text
    assert "В очереди: 1 одобренных" in text
    assert "500 000 из 2 000 000 (25%)" in text
    assert run_digest(rt)["status"] == "already_sent"
    assert len(rt.notifier.sent) == 1


def test_late_run_after_midnight_reports_previous_day(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, now=NIGHT + timedelta(hours=1, minutes=30))  # 01:15 МСК следующего дня
    assert digest_day(rt).isoformat() == "2026-10-07"
