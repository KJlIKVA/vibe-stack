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
    assert "Модель" not in text  # ошибок модели нет — строки о них не выводятся
    assert run_digest(rt)["status"] == "already_sent"
    assert len(rt.notifier.sent) == 1


def test_run_delayed_until_morning_reports_previous_day(cfg, tmp_path) -> None:
    """Решение 73: GitHub запускал сводку в 04:11, и она считала только начавшийся день («вышло 0»)."""
    rt = make_rt(cfg, tmp_path, now=NIGHT + timedelta(hours=5))  # 04:45 МСК следующего дня
    assert digest_day(rt).isoformat() == "2026-10-07"


def test_old_key_of_the_same_date_does_not_block_digest(cfg, tmp_path) -> None:
    """Решение 73: под старым ключом за эту дату записана сводка, вышедшая утром этого дня."""
    rt = make_rt(cfg, tmp_path)
    rt.mark_done("digest:2026-10-07")
    assert run_digest(rt)["status"] == "sent"


def test_late_run_after_midnight_reports_previous_day(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, now=NIGHT + timedelta(hours=1, minutes=30))  # 01:15 МСК следующего дня
    assert digest_day(rt).isoformat() == "2026-10-07"


def test_llm_unavailable_notifies_admin_once_a_day(cfg, tmp_path) -> None:
    from vibe_stack.llm import LLMUnavailable

    rt = make_rt(cfg, tmp_path)
    err = LLMUnavailable("score: AuthenticationError: expired_secret_key")
    rt.llm_unavailable("сбор", err)
    rt.llm_unavailable("сбор", err)
    assert len(rt.notifier.sent) == 1
    assert "OPENAI_API_KEY" in rt.notifier.sent[0]
    rt.clock = lambda: NIGHT + timedelta(days=1)  # следующий день по времени канала
    rt.llm_unavailable("сбор", err)
    assert len(rt.notifier.sent) == 2


def _failed_calls(rt, n: int, ok: bool) -> None:
    for _ in range(n):
        rt.state.record_llm_call(run_id="x", step="score", model="gpt-5.6-terra", now=NIGHT - timedelta(hours=1),
                                 day=digest_day(rt), input_tokens=0, cached_tokens=0, output_tokens=0,
                                 cost_usd=0.0, ok=ok)


def test_digest_warns_when_model_never_answered(cfg, tmp_path) -> None:
    from vibe_stack.digest import build_digest

    rt = make_rt(cfg, tmp_path)
    _failed_calls(rt, 5, ok=False)
    text = build_digest(rt, digest_day(rt))
    assert "⚠️ Модель не ответила ни разу (ошибок: 5)" in text


def test_digest_counts_model_errors_next_to_successes(cfg, tmp_path) -> None:
    from vibe_stack.digest import build_digest

    rt = make_rt(cfg, tmp_path)
    _failed_calls(rt, 3, ok=True)
    _failed_calls(rt, 2, ok=False)
    text = build_digest(rt, digest_day(rt))
    assert "⚠️ Модель: ошибок 2, удачных вызовов 3" in text
