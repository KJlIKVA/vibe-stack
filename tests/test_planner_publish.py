from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from vibe_stack.dayplan import slot_grid, spread
from vibe_stack.models import PostRecord, Status
from vibe_stack.planner import pick_next, same_topic, topic_tokens
from vibe_stack.storage import PublishedRow
from vibe_stack.timeutil import local_date

MSK = ZoneInfo("Europe/Moscow")


def post(title: str, rubric: str, url: str, score: int = 12, hours_ago: float = 5, now=None) -> PostRecord:
    return PostRecord(ref=title, title=title, rubric=rubric, status=Status.APPROVED, score=score, source_url=url,
                      found_at=(now - timedelta(hours=hours_ago)) if now else None)


def published(title: str, rubric: str, url: str, at: datetime, urgent: bool = False) -> PublishedRow:
    from vibe_stack.urls import host_of

    return PublishedRow(ref=title, rubric=rubric, urgent=urgent, title=title, source_url=url, domain=host_of(url),
                        published_at=at, local_date=local_date(at, "Europe/Moscow"), slot=None, tg_message_id=1,
                        counts_regular=not urgent)


ENABLED = {k: True for k in ("tool", "skill_mcp", "trick", "case", "urgent", "book_video", "analysis")}


def pick(queue, history, cfg, now):
    return pick_next(queue, history, today=local_date(now, "Europe/Moscow"), now=now, cfg=cfg.planner,
                     rubric_enabled=ENABLED)


def test_same_rubric_not_twice_in_a_row(cfg, now) -> None:
    hist = [published("Old tool", "tool", "https://a.dev/1", now - timedelta(hours=4))]
    q = [post("Another tool", "tool", "https://b.dev/2", score=15, now=now),
         post("Nice trick", "trick", "https://c.dev/3", score=10, now=now)]
    assert pick(q, hist, cfg, now).post.title == "Nice trick"


def test_posts_per_domain_per_day(cfg, now) -> None:
    cfg.planner.max_per_domain_per_day = 2
    hist = [published(f"Old {i}", "trick", f"https://openai.com/index/{i}", now - timedelta(hours=2 + i))
            for i in range(2)]
    q = [post("News X", "tool", "https://openai.com/index/x", now=now)]
    p = pick(q, hist, cfg, now)
    assert p.post is None and "домен openai.com" in p.reason


def test_github_domain_is_repo_owner(cfg, now) -> None:
    cfg.planner.max_per_domain_per_day = 1
    hist = [published("Old", "trick", "https://github.com/a/b", now - timedelta(hours=2))]
    same_owner = pick([post("Repo Y", "tool", "https://github.com/a/y", now=now)], hist, cfg, now)
    assert same_owner.post is None and "домен github.com/a" in same_owner.reason
    assert pick([post("Repo X", "tool", "https://github.com/x/y", now=now)], hist, cfg, now).post is not None


def test_topic_not_repeated_within_7_days(cfg, now) -> None:
    hist = [published("Claude Code hooks guide for beginners", "trick", "https://a.dev/hooks",
                      now - timedelta(days=3))]
    q = [post("Claude Code hooks guide for beginners (updated)", "tool", "https://b.dev/hooks2", now=now)]
    assert pick(q, hist, cfg, now).post is None
    old = [published("Claude Code hooks guide for beginners", "trick", "https://a.dev/hooks",
                     now - timedelta(days=9))]
    assert pick(q, old, cfg, now).post is not None


def test_weekly_max_and_min_quotas(cfg, now) -> None:
    week = [published(f"Book {i}", "book_video", f"https://pub{i}.dev/b", now - timedelta(hours=30 + i))
            for i in range(2)]
    q = [post("Book 3", "book_video", "https://pub3.dev/b", now=now)]
    assert pick(q, week, cfg, now).post is None  # больше 2 книг/видео в неделю нельзя
    q2 = [post("Big tool", "tool", "https://t.dev/x", score=15, now=now),
          post("Почему агенты ошибаются", "analysis", "https://a.dev/why", score=0, now=now)]
    assert pick(q2, [], cfg, now).post.rubric == "analysis"  # недельный минимум важнее баллов


def test_score_then_waiting_time(cfg, now) -> None:
    q = [post("Low", "tool", "https://a.dev/1", score=10, now=now),
         post("High", "trick", "https://b.dev/2", score=14, now=now)]
    assert pick(q, [], cfg, now).post.title == "High"


def test_empty_queue_reason(cfg, now) -> None:
    assert pick([], [], cfg, now).reason == "очередь пуста"


def test_topic_tokens_repo_match() -> None:
    a = topic_tokens("acme/tool v2 released", "https://github.com/acme/tool")
    b = topic_tokens("Something else entirely", "https://github.com/acme/tool")
    assert same_topic(a, b, 0.9)


def test_official_releases_go_first_and_are_not_blocked(cfg, now) -> None:
    """Решение 63. До 13.10 выпуски claude-code v2.1.294–296 и codex 0.162.1 стояли в очереди и устаревали:
    «тема была за 7 дней» (выпуск того же репозитория уже вышел), а посты openai.com упирались в лимит домена."""
    from vibe_stack.planner import Official

    official = Official.from_sources(cfg.sources)
    rel = "https://github.com/anthropics/claude-code/releases/tag/"
    hist = [published("anthropics/claude-code v2.1.293", "tool", rel + "v2.1.293", now - timedelta(days=2))]
    hist += [published(f"OpenAI story {w}", "case", f"https://openai.com/index/{w}", now - timedelta(hours=h))
             for h, w in [(1, "alpha"), (2, "beta"), (3, "gamma")]]
    release = post("anthropics/claude-code v2.1.296", "tool", rel + "v2.1.296", score=10, now=now)
    news = post("Sophos cuts investigation time", "case", "https://openai.com/index/sophos", score=11, now=now)
    other = post("A terminal for agents", "tool", "https://x.dev/term", score=15, now=now)
    kw = {"today": local_date(now, "Europe/Moscow"), "now": now, "cfg": cfg.planner, "rubric_enabled": ENABLED,
          "soft_rubric_repeat": True}
    old = pick_next([release, news, other], hist, **kw)  # без признака «официальный» — как было
    assert old.post is other and set(old.skipped) == {release.ref, news.ref}
    res = pick_next([release, news, other], hist, official=official, **kw)
    assert res.post is release and not res.skipped  # официальное — раньше поста с баллом выше
    # …и раньше поста другой рубрики: последним выходил «case», но Sophos всё равно первый
    assert pick_next([news, other], hist, official=official, **kw).post is news
    # повтор есть повтор: пересказ того же выпуска в чужом блоге и тот же выпуск ещё раз — не выходят
    retell = post("anthropics/claude-code v2.1.293 released", "tool", "https://blog.dev/cc-293", now=now)
    same = post("anthropics/claude-code v2.1.293", "tool", rel + "v2.1.293", now=now)
    assert pick_next([retell, same], hist, official=official, **kw).post is None
    # чужой репозиторий официальным не становится: лимит домена и правило репозитория для него прежние
    assert not official("https://github.com/acme/tool") and official(rel + "v2.1.296")


def test_spread_evenly_over_free_slots() -> None:
    grid = slot_grid(datetime(2026, 10, 7).date(), [f"{h:02d}:{m:02d}" for h in range(8, 24) for m in (0, 30)],
                     "Europe/Moscow")
    times = [f"{t:%H:%M}" for t in spread(grid, 8)]
    assert times == ["08:00", "10:00", "12:30", "14:30", "17:00", "19:00", "21:30", "23:30"]
    assert [f"{t:%H:%M}" for t in spread(grid, 1)] == ["08:00"]  # один пост — в ближайший слот
    assert len(spread(grid, 40)) == 32


def test_soft_rubric_repeat_prefers_other_rubric(cfg, now) -> None:
    hist = [published("Old", "tool", "https://a.dev/x", now - timedelta(hours=2))]
    q = [post("Tool B", "tool", "https://b.dev/x", score=14, now=now),
         post("Trick C", "trick", "https://c.dev/x", score=10, now=now)]
    assert pick(q, hist, cfg, now).post.title == "Trick C"
    only_tools = [post("Tool B", "tool", "https://b.dev/x", now=now)]
    assert pick(only_tools, hist, cfg, now).post is None  # строго: та же рубрика подряд
    soft = pick_next(only_tools, hist, today=local_date(now, "Europe/Moscow"), now=now, cfg=cfg.planner,
                     rubric_enabled=ENABLED, soft_rubric_repeat=True)
    assert soft.post.title == "Tool B"  # в плане дня допускается, если других нет
