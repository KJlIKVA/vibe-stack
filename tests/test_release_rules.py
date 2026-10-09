"""Решение 61: только вышедшие книги, без pull request и issue; понятный пост о бенчмарке."""

from __future__ import annotations

from datetime import datetime

import pytest

from vibe_stack import prompts
from vibe_stack.guards import github_work_in_progress, unreleased_book
from vibe_stack.models import Candidate, FetchedDoc
from vibe_stack.steps import doc_guards, prefilter

MEAP = ("Agent Design Patterns Elements, components, and architecture Peter Belcak MEAP began August 2026 "
        "Last updated September 2026 Publication in Spring 2027 ( estimated ) ISBN 9781633434318")
MANNING_RELEASED = ("Financial AI in Practice Taehun Kim October 2026 ISBN 9781633435391 464 pages "
                    "access to all Manning books, MEAPs, liveVideos MEAP liveBook liveVideo")


@pytest.mark.parametrize(("text", "unreleased"), [
    (MEAP, True),
    (MANNING_RELEASED, False),  # «MEAP» в меню и подписке сайта — не метка книги
    ("Mastering LangChain and LangGraph Due: 12 November 2026 Softcover ISBN", True),
    ("Softcover ISBN: 979-8-8688-2945-1Published: 24 September 2026", False),
    ("Prompt Engineering for Developers, Early Release, raw and unedited", True),
    ("This book is in beta. Get it now and get updates as the author writes", True),
])
def test_unreleased_book_markers(text: str, unreleased: bool) -> None:
    assert unreleased_book(text) is unreleased


@pytest.mark.parametrize(("url", "wip"), [
    ("https://github.com/ggml-org/llama.cpp/pull/29928", True),
    ("https://github.com/ggml-org/llama.cpp/issues/100", True),
    ("https://github.com/ggml-org/llama.cpp/commit/abc123", True),
    ("https://github.com/ggml-org/llama.cpp", False),
    ("https://github.com/ggml-org/llama.cpp/releases/tag/b9000", False),
    ("https://example.com/owner/repo/pull/1", False),
])
def test_github_work_in_progress(url: str, wip: bool) -> None:
    assert github_work_in_progress(url) is wip


def test_pull_request_is_rejected_before_fetch(cfg, now) -> None:
    c = Candidate(source="hackernews", source_type="hackernews", title="llama : add a GPU cache for MoE experts",
                  url="https://github.com/ggml-org/llama.cpp/pull/29928")
    assert "not_released" in prefilter(c, cfg, now)


def _doc(text: str, now: datetime) -> FetchedDoc:
    return FetchedDoc(url="https://www.manning.com/books/x", final_url="https://www.manning.com/books/x", ok=True,
                      fetched_at=now, text=text)


def test_book_in_early_access_is_rejected_by_code(cfg, now) -> None:
    assert "not_released" in doc_guards(_doc(MEAP, now), cfg, now, 180, book=True)
    assert "not_released" not in doc_guards(_doc(MANNING_RELEASED, now), cfg, now, 180, book=True)
    assert "not_released" not in doc_guards(_doc(MEAP, now), cfg, now, 180)  # не книга — правило не действует


def test_prompts_follow_decision_61(now) -> None:
    score = prompts.score_prompt({"id": "x", "title": "t"}, "doc", now.date())
    assert "только вышедшие книги" in score and "для начинающих" in score
    assert "Ранний доступ (MEAP, Early Release) подходит" not in score
    assert "Pull request" in score
    bench = prompts.write_prompt(["c1", "c2"], {"category": "benchmark", "url": "u", "title": "t", "mode": "standard"},
                                 "benchmark", "📊", "#бенчмарк", notes=prompts.rubric_notes("benchmark"))
    assert "Вместо заголовка «Что измеряет этот бенчмарк»" in bench and "Зачем это вам" in bench
    assert "раннем доступе" not in prompts.load("book_video_notes")


def test_today_is_written_unambiguously() -> None:
    """09.10.2026 модель читала как 10 сентября: книги, вышедшие в сентябре, казались «будущими»."""
    from datetime import date

    assert prompts.human_date(date(2026, 10, 9)) == "9 октября 2026 года"
    assert "Сегодня 9 октября 2026 года." in prompts.score_prompt({"id": "x"}, "doc", date(2026, 10, 9))
