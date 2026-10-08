"""Адаптеры рейтингов на MockTransport: места только из данных источника."""

from __future__ import annotations

import httpx

from vibe_stack.config import LeaderboardConfig
from vibe_stack.leaderboards import ArenaAdapter, ArtificialAnalysisAdapter


def row(rank, name, cat="overall", date="2026-10-02"):
    return {"row": {"rank": rank, "model_name": name, "category": cat, "leaderboard_publish_date": date}}


def arena_with(pages):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(int(req.url.params["offset"]))
        return httpx.Response(200, json={"rows": pages[len(calls) - 1] if len(calls) <= len(pages) else []})

    cfg = LeaderboardConfig(key="arena_text", type="arena_hf", label="Arena · текст",
                            dataset_config="text_style_control")
    return ArenaAdapter(cfg, httpx.Client(transport=httpx.MockTransport(handler))), calls


def test_arena_top3_by_rank_from_overall_only() -> None:
    page = [row(2.0, "b"), row(1.0, "a"), row(1, "x", cat="coding"), row(3, "c"), row(4, "d")]
    a, calls = arena_with([page])
    s = a.fetch()
    assert s.top == ["a", "b", "c"] and s.date == "2026-10-02" and calls == [0]
    assert "CC BY 4.0" in s.attribution


def test_arena_pages_until_ranks_found() -> None:
    a, calls = arena_with([[row(1, "a"), row(2, "b")], [row(3, "c")]])
    assert a.fetch().top == ["a", "b", "c"] and calls == [0, 100]


def test_arena_category_block_after_overall() -> None:
    """Кодинг — категория coding: её блок идёт после overall и других категорий (решение 54)."""
    calls = []
    pages = [[row(1, "o1"), row(2, "o2")], [row(5, "zh", cat="chinese")],
             [row(1, "c1", cat="coding"), row(2, "c2", cat="coding")], [row(3, "c3", cat="coding")],
             [row(1, "w", cat="creative_writing")]]

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(int(req.url.params["offset"]))
        return httpx.Response(200, json={"rows": pages[len(calls) - 1] if len(calls) <= len(pages) else []})

    cfg = LeaderboardConfig(key="arena_coding", type="arena_hf", label="Кодинг", dataset_config="text_style_control",
                            category="coding")
    s = ArenaAdapter(cfg, httpx.Client(transport=httpx.MockTransport(handler))).fetch()
    assert s.top == ["c1", "c2", "c3"] and calls == [0, 100, 200, 300]
    # блок категории кончился, а мест 1–3 нет — дальше не листаем
    calls.clear()
    pages[3] = [row(9, "w", cat="creative_writing")]
    assert ArenaAdapter(cfg, httpx.Client(transport=httpx.MockTransport(handler))).fetch() is None
    assert calls == [0, 100, 200, 300]


def test_arena_missing_ranks_or_mixed_dates_gives_none() -> None:
    a, _ = arena_with([[row(1, "a"), row(2, "b"), row(4, "d")], []])
    assert a.fetch() is None  # третьего места нет — ничего не выдумываем
    a, _ = arena_with([[row(1, "a"), row(2, "b"), row(3, "c", date="2026-09-01")]])
    assert a.fetch() is None


def test_aa_requires_key_and_sorts_by_index(monkeypatch, now) -> None:
    cfg = LeaderboardConfig(key="aa_index", type="artificial_analysis", label="Artificial Analysis")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-api-key"] == "k"
        data = [{"name": n, "evaluations": {"artificial_analysis_intelligence_index": v}}
                for n, v in [("m1", 50), ("m2", 70.5), ("m3", None), ("m4", 60), ("m5", 65)]]
        return httpx.Response(200, json={"data": data})

    a = ArtificialAnalysisAdapter(cfg, httpx.Client(transport=httpx.MockTransport(handler)), lambda: now,
                                  "Europe/Moscow")
    monkeypatch.delenv("AA_API_KEY", raising=False)
    assert a.fetch() is None
    monkeypatch.setenv("AA_API_KEY", "k")
    s = a.fetch()
    assert s.top == ["m2", "m5", "m4"] and "Artificial Analysis" in s.attribution


def test_aa_disabled_by_default(cfg) -> None:
    from vibe_stack.leaderboards import build_adapters

    assert [a.key for a in build_adapters(cfg)] == ["arena_text", "arena_coding", "arena_webdev", "arena_agent",
                                                    "arena_image", "arena_video"]


def test_arena_without_valid_date_gives_none() -> None:
    a, _ = arena_with([[row(1, "A", date=None), row(2, "B", date=None), row(3, "C", date=None)]])
    assert a.fetch() is None  # «данные на None» не пишем
    b, _ = arena_with([[row(1, "A", date="2026-10-02T00:00:00"), row(2, "B", date="2026-10-02"),
                     row(3, "C", date="2026-10-02")]])
    assert b.fetch().date == "2026-10-02"
