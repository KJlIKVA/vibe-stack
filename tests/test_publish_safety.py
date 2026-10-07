"""Публикация не превышает лимиты и не даёт дублей даже при потере состояния и сбоях Telegram/Notion."""

from __future__ import annotations

from datetime import timedelta

import pytest

from vibe_stack.board import BoardUnavailable, LocalBoard
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.llm import FakeLLM
from vibe_stack.models import PostRecord, Status
from vibe_stack.publish import run_publish
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier, TelegramError

HTML = ('🛠 <b>{t}</b>\n\nСуть.\n<a href="{u}">Первоисточник</a>\n✅ Сверено с первоисточником · #инструмент')


class FlakyTG(DryRunTelegram):
    def __init__(self, out, error: TelegramError | None) -> None:
        super().__init__(out)
        self.error = error
        self.attempts = 0

    def send_message(self, chat_id, text, *, html=True, preview=True, preview_url=None) -> int:
        self.attempts += 1
        if self.error:
            raise self.error
        return super().send_message(chat_id, text)


@pytest.fixture(autouse=True)
def three_slots(cfg) -> None:
    """Сценарии написаны под расписание «3 поста: 10/14/18, окно 120 минут» — не зависят от config.yaml."""
    cfg.schedule.publish_slots = ["10:00", "14:00", "18:00"]
    cfg.schedule.slot_window_minutes = 120
    cfg.limits.regular_per_day = 3


def make_rt(cfg, tmp_path, now, tg=None, board=None) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    return Runtime(cfg=cfg, state=state, board=board or LocalBoard(tmp_path / "b.json"),
                   tg=tg or DryRunTelegram(tmp_path / "tg"), notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                   clock=clock, run_id="p", out_dir=tmp_path, mode="dry-run", channel_id="@c",
                   sources_factory=lambda _: [])


def approved(board: LocalBoard, now, title="Tool A", url="https://github.com/a/tool", **kw) -> str:
    rubric, status = kw.pop("rubric", "tool"), kw.pop("status", Status.APPROVED)
    return board.add_post(PostRecord(title=title, rubric=rubric, status=status, source_url=url,
                                     source_domain="github.com", score=12, found_at=now - timedelta(hours=2),
                                     html=HTML.format(t=title, u=url), **kw))


def test_lost_state_does_not_reopen_filled_slot(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    board = rt.board
    # в Notion пост уже вышел в этом слоте (10:20 МСК), а состояние пустое — как после потери ветки state
    board.add_post(PostRecord(title="Earlier", rubric="trick", status=Status.PUBLISHED, source_url="https://x.dev/1",
                              source_domain="x.dev", published_at=now - timedelta(minutes=10), html="x"))
    approved(board, now)
    assert run_publish(rt)["status"] == "slot_10:00_done"
    assert rt.tg.sent == []


def test_daily_limit_counted_from_board_too(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    for i in range(3):  # три обычных уже вышли сегодня ночью/утром по данным Notion
        rt.board.add_post(PostRecord(title=f"P{i}", rubric="trick", status=Status.PUBLISHED,
                                     source_url=f"https://x{i}.dev", published_at=now - timedelta(hours=5 + i),
                                     html="x"))
    approved(rt.board, now)
    assert run_publish(rt)["status"] == "daily_limit_reached"


def test_uncertain_send_is_not_retried_and_blocks_slot(cfg, tmp_path, now) -> None:
    tg = FlakyTG(tmp_path / "tg", TelegramError("sendMessage: ответ не получен", retryable=False, uncertain=True))
    rt = make_rt(cfg, tmp_path, now, tg=tg)
    ref = approved(rt.board, now)
    other = approved(rt.board, now, title="Trick B", url="https://b.dev/trick", rubric="trick")
    s = run_publish(rt)
    assert s["status"] == "error" and tg.attempts == 1
    assert rt.board.get(ref).status == Status.ERROR
    assert "неизвестно" in rt.notifier.sent[0]
    # следующий запуск в том же слоте не отправляет второй пост и не повторяет первый
    rt._settings = None
    tg.error = None
    assert run_publish(rt)["status"] == "slot_10:00_done"
    assert tg.attempts == 1 and rt.board.get(other).status == Status.APPROVED


def test_no_send_without_sending_marker(cfg, tmp_path, now) -> None:
    class NoWrites(LocalBoard):
        def update_post(self, ref, **fields):
            raise BoardUnavailable("Notion: не удалось обновить строку поста")

    board = NoWrites(tmp_path / "b.json")
    rt = make_rt(cfg, tmp_path, now, board=board)
    approved(board, now)
    assert run_publish(rt)["status"] == "error"
    assert rt.tg.sent == []


def test_sending_marker_then_published(cfg, tmp_path, now) -> None:
    seen: list[str] = []

    class Spy(LocalBoard):
        def update_post(self, ref, **fields):
            seen.append(fields.get("status"))
            super().update_post(ref, **fields)

    board = Spy(tmp_path / "b.json")
    rt = make_rt(cfg, tmp_path, now, board=board)
    ref = approved(board, now)
    assert run_publish(rt)["status"] == "published"
    assert seen == [Status.SENDING, Status.PUBLISHED]
    assert board.get(ref).tg_message_id == -1


def test_urgent_rows_are_left_to_urgent_contour(cfg, tmp_path, now) -> None:
    rt = make_rt(cfg, tmp_path, now)
    approved(rt.board, now, urgent=True, rubric="urgent")
    assert run_publish(rt)["status"] == "nothing_to_publish"


@pytest.mark.parametrize(("field", "value"), [("publish_slots", ["10.00"]), ("tz", "Mars/Olympus")])
def test_bad_board_settings_fail_closed(cfg, tmp_path, now, field, value) -> None:
    rt = make_rt(cfg, tmp_path, now)
    rt.board.set_settings(**{field: value})
    approved(rt.board, now)
    assert run_publish(rt)["status"] == "board_unavailable"
    assert rt.tg.sent == []
