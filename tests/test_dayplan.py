"""План дня: время каждому посту, публикация по времени, правки в Notion, опоздания и пауза."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from vibe_stack.board import LocalBoard
from vibe_stack.dayplan import plan_and_report
from vibe_stack.fetch import FixtureFetcher
from vibe_stack.llm import FakeLLM
from vibe_stack.models import PostRecord, Status
from vibe_stack.publish import run_publish
from vibe_stack.runtime import Runtime
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier

MSK = ZoneInfo("Europe/Moscow")
MORNING = datetime(2026, 10, 7, 7, 15, tzinfo=MSK).astimezone(UTC)


def make_rt(cfg, tmp_path, at: datetime) -> Runtime:
    state = State(tmp_path / "s.db")
    clock = {"now": at}
    rt = Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                 notifier=Notifier(None, None, tmp_path / "a.log"),
                 llm=FakeLLM(cfg.llm, state, "p", lambda: clock["now"], "Europe/Moscow", {}),
                 fetcher=FixtureFetcher({}, lambda: clock["now"]), clock=lambda: clock["now"], run_id="p",
                 out_dir=tmp_path, mode="publish", channel_id="@c", sources_factory=lambda _: [])
    rt._clock_box = clock  # type: ignore[attr-defined]
    return rt


def at(rt: Runtime, hh: int, mm: int) -> None:
    rt._clock_box["now"] = datetime(2026, 10, 7, hh, mm, tzinfo=MSK).astimezone(UTC)  # type: ignore[attr-defined]
    rt._settings = None


def add(rt: Runtime, title: str, rubric: str, url: str, **kw) -> str:
    html = (f'🛠 <b>{title}</b>\n\nСуть.\n<a href="{url}">Первоисточник</a>\n'
            '✅ Сверено с первоисточником · #инструмент')
    return rt.board.add_post(PostRecord(title=title, rubric=rubric, status=Status.APPROVED, source_url=url,
                                        source_domain=url.split("/")[2], score=12, html=html,
                                        found_at=rt.now() - timedelta(minutes=10), **kw))


def hhmm(t: datetime | None) -> str:
    assert t is not None
    return f"{t.astimezone(MSK):%H:%M}"


def test_morning_plan_spreads_posts_and_reports(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    refs = [add(rt, "Tool A", "tool", "https://a.dev/x"), add(rt, "Trick B", "trick", "https://b.dev/x"),
            add(rt, "Case C", "case", "https://c.dev/x")]
    assert plan_and_report(rt)["planned"] == 3
    times = sorted(hhmm(rt.board.get(r).planned_at) for r in refs)
    assert times == ["08:00", "16:00", "23:30"]  # равномерно по дню, с 08:00 до 23:30
    text = rt.notifier.sent[-1]
    assert "План на 07.10: 3" in text and "08:00" in text and "Время публикации" in text


def test_posts_go_out_at_their_time_and_notion_edits_win(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    a = add(rt, "Tool A", "tool", "https://a.dev/x")
    b = add(rt, "Trick B", "trick", "https://b.dev/x")
    plan_and_report(rt)
    first, second = sorted([a, b], key=lambda r: rt.board.get(r).planned_at)
    at(rt, 8, 7)
    s = run_publish(rt)
    assert s["status"] == "published" and s["titles"] == [rt.board.get(first).title]
    at(rt, 8, 37)
    assert run_publish(rt)["status"] == "nothing_to_publish"
    # вы передвинули второй пост в Notion на 09:00 — он выходит в 09:00, а не по плану бота
    rt.board.update_post(second, planned_at=datetime(2026, 10, 7, 9, 0, tzinfo=MSK))
    at(rt, 9, 7)
    assert run_publish(rt)["status"] == "published"
    assert rt.board.get(second).status == Status.PUBLISHED


def test_rejected_in_notion_is_not_published(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    ref = add(rt, "Tool A", "tool", "https://a.dev/x")
    plan_and_report(rt)
    rt.board.update_post(ref, status=Status.REJECTED)
    at(rt, 8, 7)
    assert run_publish(rt)["status"] == "nothing_to_publish" and rt.tg.sent == []


def test_late_tick_publishes_at_most_two(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    for i, (title, rubric) in enumerate(zip(["Alpha linter", "Beta tracing", "Gamma deploys"],
                                            ["tool", "trick", "case"], strict=True)):
        add(rt, title, rubric, f"https://d{i}.dev/x",
            planned_at=datetime(2026, 10, 7, 8, 0, tzinfo=MSK) + timedelta(minutes=10 * i))
    at(rt, 8, 45)  # тик опоздал: наступило время трёх постов
    assert run_publish(rt)["published"] == 2
    at(rt, 9, 7)
    assert run_publish(rt)["published"] == 1


def test_long_overdue_posts_are_replanned_not_dumped(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    titles = ["Alpha linter", "Beta tracing", "Gamma deploys"]
    refs = [add(rt, t, r, f"https://e{i}.dev/x", planned_at=datetime(2026, 10, 7, 8, 0, tzinfo=MSK))
            for i, (t, r) in enumerate(zip(titles, ["tool", "trick", "case"], strict=True))]
    at(rt, 12, 10)  # например, весь утро стояла пауза
    s = run_publish(rt)
    assert s["published"] == 1  # один — в текущий свободный слот 12:00, остальные получили новое время
    later = sorted(hhmm(rt.board.get(r).planned_at) for r in refs if rt.board.get(r).status == Status.APPROVED)
    assert later and all(t > "12:00" for t in later)


def test_pause_means_no_plan_and_no_posts(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    ref = add(rt, "Tool A", "tool", "https://a.dev/x")
    rt.board.set_settings(pause=True)
    assert plan_and_report(rt)["status"] == "paused"
    assert rt.board.get(ref).planned_at is None


def test_daily_limit_caps_the_plan(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    rt.board.set_settings(regular_per_day=2)
    titles = ["Alpha linter", "Beta tracing", "Gamma deploys"]
    refs = [add(rt, t, r, f"https://f{i}.dev/x") for i, (t, r) in enumerate(zip(titles, ["tool", "trick", "case"],
                                                                             strict=True))]
    assert plan_and_report(rt)["planned"] == 2
    assert sum(1 for r in refs if rt.board.get(r).planned_at is None) == 1  # третий ждёт следующего дня


def test_overdue_post_without_new_slot_does_not_go_out(cfg, tmp_path) -> None:
    rt = make_rt(cfg, tmp_path, MORNING)
    old = datetime(2026, 10, 7, 8, 0, tzinfo=MSK)
    a = add(rt, "Same topic words here", "tool", "https://g1.dev/x", planned_at=old)
    b = add(rt, "Same topic words here again", "trick", "https://g2.dev/x", planned_at=old)
    at(rt, 12, 10)
    assert run_publish(rt)["published"] == 1  # второй — та же тема, места в плане ему нет
    left = [r for r in (a, b) if rt.board.get(r).status == Status.APPROVED]
    assert len(left) == 1 and rt.board.get(left[0]).planned_at is None
