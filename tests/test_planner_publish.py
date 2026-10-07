from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from vibe_stack.models import PostRecord, Status
from vibe_stack.planner import pick_next, same_topic, topic_tokens
from vibe_stack.publish import active_slot
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


ENABLED = {k: True for k in ("tool", "skill_mcp", "trick", "case", "urgent", "book_video", "glossary", "analysis")}


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
          post("Слово дня: MCP", "glossary", "https://modelcontextprotocol.io/x", score=0, now=now)]
    assert pick(q2, [], cfg, now).post.rubric == "glossary"  # недельный минимум важнее баллов


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


def test_active_slot() -> None:
    slots = ["10:00", "14:00", "18:00"]
    assert active_slot(datetime(2026, 10, 7, 10, 30, tzinfo=MSK), slots, 120) == "10:00"
    assert active_slot(datetime(2026, 10, 7, 12, 30, tzinfo=MSK), slots, 120) is None
    assert active_slot(datetime(2026, 10, 7, 9, 59, tzinfo=MSK), slots, 120) is None
    assert active_slot(datetime(2026, 10, 7, 18, 1, tzinfo=MSK), slots, 120) == "18:00"


def test_glossary_terms_are_different_topics(cfg, now) -> None:
    hist = [published("Слово дня: LLM", "glossary", "https://a.dev/llm", now - timedelta(days=3)),
            published("Some tool", "tool", "https://t.dev/x", now - timedelta(hours=3))]
    other = [post("Слово дня: RAG", "glossary", "https://b.dev/rag", now=now)]
    same = [post("Слово дня: LLM", "glossary", "https://c.dev/llm", now=now)]
    assert pick(other, hist, cfg, now).post is not None
    assert pick(same, hist, cfg, now).post is None
