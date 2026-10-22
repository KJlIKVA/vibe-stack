"""Решение 70: в канал — только действительно интересное. Порог интереса, новые источники (claude.com без дат,
GitHub Trending), записи changelog одного дня — один пост, переименованный репозиторий — тот же проект."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from vibe_stack import collect, prompts
from vibe_stack.config import SourceConfig
from vibe_stack.fetch import HttpFetcher
from vibe_stack.llm import SCHEMAS
from vibe_stack.models import Candidate, ScoreResult
from vibe_stack.planner import pick_next, same_news, same_topic, topic_tokens
from vibe_stack.sources import md_changelog
from vibe_stack.sources.github import GitHubTrendingSource, parse_trending
from vibe_stack.sources.rss import RSSSource
from vibe_stack.sources.sitemap import SitemapSource
from vibe_stack.steps import gate, merge_batch
from vibe_stack.storage import State

from .test_planner_publish import ENABLED, post
from .test_sources_v2 import CHANGELOG, PAGE, client


def score(category: str = "tool", interest: int = 4) -> ScoreResult:
    return ScoreResult.model_validate({"id": "x", "category": category, "scores": {
        "novelty": 3, "usefulness": 4, "verifiability": 3, "substance": 2, "audience_fit": 2, "interest": interest}})


# --- порог интереса ---------------------------------------------------------------------------
def test_fresh_and_verifiable_is_not_enough(cfg) -> None:
    """«GitHub CLI: истекающие токены» набирал 14 из 15: свежо, проверяемо, конкретно — но неинтересно."""
    rubrics = {k: v.model_copy(update={"enabled": True}) for k, v in cfg.rubrics.items()}
    assert gate(score(interest=1), cfg, rubrics).reasons == ["low_interest"]
    assert gate(score(interest=3), cfg, rubrics).reasons == ["low_interest"]
    assert gate(score(interest=4), cfg, rubrics).passed
    assert gate(score("book_video", interest=0), cfg, rubrics, media="book").passed  # у книг и видео свои пороги


def test_missing_interest_fails_closed(cfg) -> None:
    rubrics = {k: v.model_copy(update={"enabled": True}) for k, v in cfg.rubrics.items()}
    old = ScoreResult.model_validate({"id": "x", "category": "tool", "scores": {
        "novelty": 3, "usefulness": 5, "verifiability": 3, "substance": 2, "audience_fit": 2}})
    assert old.scores.total == 15 and gate(old, cfg, rubrics).reasons == ["low_interest"]


def test_model_must_return_interest() -> None:
    scores = SCHEMAS["score"]["properties"]["scores"]
    assert "interest" in scores["required"] and scores["properties"]["interest"]["enum"] == list(range(6))
    out = prompts.score_prompt({"id": "x"}, "doc")
    assert '"interest" 0–5' in out and "что это и кто это сделал" in out


def test_planner_takes_more_interesting_first(now) -> None:
    dull = post("Dull", "tool", "https://a.dev/1", score=15, now=now)
    hot = post("Hot", "tool", "https://b.dev/2", score=11, now=now)
    dull.scores, hot.scores = {"interest": 4}, {"interest": 5}
    pick = pick_next([dull, hot], [], today=now.date(), now=now, cfg=_planner(), rubric_enabled=ENABLED)
    assert pick.post is hot


def _planner():
    from vibe_stack.config import load_config

    from .conftest import ROOT
    return load_config(ROOT / "config.yaml").planner


def test_writing_rules_without_dash_glossary() -> None:
    clarity = prompts.load("clarity")
    assert "2–5 словах" not in clarity and "не поясняй" in clarity and "кто это сделал" in clarity
    notes = prompts.load("standard_notes")
    assert "Заголовок называет продукт" in notes


# --- claude.com: sitemap без дат ---------------------------------------------------------------------------
UNDATED = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://claude.com/resources/articles/old-guide</loc></url>
<url><loc>https://claude.com/resources/articles/claude-duet</loc></url>
<url><loc>https://claude.com/marketplace/connectors/x</loc></url>
{extra}
</urlset>"""


def test_undated_sitemap_remembers_first_seen(tmp_path, now) -> None:
    cfg = SourceConfig(name="claude-articles", type="sitemap", url="https://claude.com/sitemap.xml",
                       path_prefix="/resources/articles/",
                       first_poll_new=["https://claude.com/resources/articles/claude-duet"])
    state = State(tmp_path / "s.db")
    since = now - timedelta(days=30)

    def poll(extra: str = "", at: datetime = now) -> list[Candidate]:
        page = httpx.Response(200, text=UNDATED.format(extra=extra))
        return SitemapSource(cfg, client({"https://claude.com/sitemap.xml": page.text}), since, state,
                             lambda: at).collect()

    # первый опрос: всё, что уже есть, — архив; кроме адреса из first_poll_new
    assert [c.url for c in poll()] == ["https://claude.com/resources/articles/claude-duet"]
    later = now + timedelta(hours=5)
    new = "<url><loc>https://claude.com/resources/articles/claude-new</loc></url>"
    got = {c.url: c for c in poll(new, later)}
    assert set(got) == {"https://claude.com/resources/articles/claude-duet",
                        "https://claude.com/resources/articles/claude-new"}
    assert got["https://claude.com/resources/articles/claude-new"].updated_at == later  # дата — первое появление
    assert got["https://claude.com/resources/articles/claude-duet"].updated_at == now  # не сдвигается
    known = json.loads(state.get("sitemap_first_seen:claude-articles") or "{}")
    assert known["https://claude.com/resources/articles/old-guide"].startswith("1970-01-01")


def test_undated_sitemap_without_state_is_skipped(now) -> None:
    cfg = SourceConfig(name="c", type="sitemap", url="https://claude.com/sitemap.xml",
                       path_prefix="/resources/articles/")
    src = SitemapSource(cfg, client({"https://claude.com/sitemap.xml": UNDATED.format(extra="")}),
                        now - timedelta(days=30))
    assert src.collect() == []


def test_claude_articles_source_is_official(cfg) -> None:
    s = next(s for s in cfg.sources if s.name == "claude-articles")
    assert s.whitelist and s.model_vendor and "urgent" in s.contours and s.official_domains == ["claude.com"]


# --- GitHub Trending ---------------------------------------------------------------------------
def _row(repo: str, desc: str, total: str, recent: str, rid: int) -> str:
    return (f'<article class="Box-row"><a data-hydro-click="{{&quot;repository_id&quot;:{rid}}}">Star</a>'
            f'<h2 class="h3 lh-condensed"> <a href="/{repo}">{repo}</a></h2>'
            f'<p class="col-9 color-fg-muted my-1 pr-4">{desc}</p>'
            f'<a href="/{repo}/stargazers"><svg></svg> {total}</a>'
            f'<span class="d-inline-block float-sm-right"><svg></svg> {recent} stars this week</span></article>')


TRENDING = (_row("morluto/rea", "Reverse engineer anything with agents", "360,282", "129,417", 1)
            + _row("NationalSecurityAgency/ghidra", "Software reverse engineering framework", "92,767", "5,769", 2)
            + _row("tiny/agent-thing", "An AI agent helper", "300", "40", 3))


def test_parse_trending() -> None:
    rows = parse_trending(TRENDING)
    assert rows[0] == {"full_name": "morluto/rea", "description": "Reverse engineer anything with agents",
                       "stars": 360282, "stars_recent": 129417, "repo_id": 1}


def test_trending_only_ai_and_popular(cfg) -> None:
    src_cfg = next(s for s in cfg.sources if s.name == "github-trending")
    got = GitHubTrendingSource(src_cfg, client({"https://github.com/trending": TRENDING})).collect()
    assert [c.title for c in got] == ["morluto/rea"]  # ghidra не про ИИ, tiny — мало звёзд за период
    assert got[0].extra["stars_recent"] == 129417 and got[0].url == "https://github.com/morluto/rea"


def test_github_projects_scored_by_stars_not_push_date(now) -> None:
    gh = [Candidate(source="gh", source_type="github_search", url=f"https://github.com/o/r{i}", title=f"o/r{i}",
                    updated_at=now - timedelta(days=i), signal=float(i)) for i in range(5)]
    chosen, _ = collect.fair_pick(gh, limit=2, per_source=8)
    assert [c.title for c in chosen] == ["o/r4", "o/r3"]


def test_merged_candidate_keeps_popularity() -> None:
    a = Candidate(source="github-search", source_type="github_search", url="https://github.com/o/r", title="o/r",
                  signal=3.0, extra={"stars": 900})
    b = Candidate(source="github-trending", source_type="github_trending", url="https://github.com/o/r",
                  title="o/r", signal=2.5, extra={"stars": 950, "stars_recent": 400})
    (m,) = merge_batch([a, b])
    assert m.extra["stars"] == 950 and m.extra["stars_recent"] == 400


# --- один проект под разными именами ---------------------------------------------------------------------------
def test_renamed_repo_is_the_same_project() -> None:
    a = topic_tokens("anujasid/blender-ai-mcp-addon", "https://github.com/anujasid/blender-ai-mcp-addon")
    b = topic_tokens("LiCinCan/blender-ai-mcp-addon", "https://github.com/LiCinCan/blender-ai-mcp-addon")
    assert same_news(a, b) and same_topic(a, b, 0.5)
    r1 = topic_tokens("anthropics/claude-code v2.1.305",
                      "https://github.com/anthropics/claude-code/releases/tag/v2.1.305")
    r2 = topic_tokens("anthropics/claude-code v2.1.306",
                      "https://github.com/anthropics/claude-code/releases/tag/v2.1.306")
    assert not same_news(r1, r2)  # два выпуска одного репозитория — разные новости


def test_repo_id_is_a_dedup_key() -> None:
    c = Candidate(source="gh", source_type="github_search", url="https://github.com/LiCinCan/x", title="LiCinCan/x",
                  extra={"repo_id": 42})
    assert "ghid:42" in c.keys


# --- changelog: один день — один пост ---------------------------------------------------------------------------
def test_changelog_entries_of_a_day_become_one_candidate(cfg, now, tmp_path) -> None:
    routes = {PAGE + ".md": CHANGELOG}
    src = md_changelog.MarkdownChangelogSource(
        SourceConfig(name="openai-api-changelog", type="md_changelog", url=PAGE, title_prefix="OpenAI API: "),
        client(routes))
    cands = src.collect()

    class RT:
        pass

    rt = RT()
    rt.cfg = cfg  # type: ignore[attr-defined]
    got = collect.group_changelog_days(rt, cands)  # type: ignore[arg-type]
    assert len(got) == 3  # 7 октября, два пункта 6 октября — один кандидат, 22 сентября
    day = next(c for c in got if "entries=" in c.url)
    assert day.title == "OpenAI API: Released the Decisions API in beta. (+1)"
    assert all(k in day.keys for c in cands if c.published_at and c.published_at.day == 6 for k in c.keys)
    doc = HttpFetcher(cfg.fetch, lambda: now, client(routes), check_urls=False).fetch(day.url, purpose="verify")
    assert doc.ok and "Decisions API" in doc.text and "usage tiers" in doc.text and "chat-latest" not in doc.text
    assert doc.published_meta == datetime(2026, 10, 6, 23, 59, tzinfo=UTC).isoformat()
    assert md_changelog.entry_ids(day.canonical) == md_changelog.entry_ids(day.url)  # переживает канонизацию


def test_release_notes_are_grouped_by_day(cfg) -> None:
    grouped = {s.name for s in cfg.sources if s.group_by_day}
    assert grouped == {"claude-release-notes", "openai-api-changelog"}


# --- служебные ленты: только про ИИ ---------------------------------------------------------------------------
FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>HydraFusion updates</title><link>https://github.blog/changelog/a</link>
<description>GitHub Copilot now orchestrates models.</description></item>
<item><title>Dependabot supports npm 12</title><link>https://github.blog/changelog/b</link>
<description>Dependency updates for npm 12.</description></item>
</channel></rss>"""


def test_include_text_regex_looks_at_description(cfg) -> None:
    s = next(s for s in cfg.sources if s.name == "github-changelog")
    got = RSSSource(s, client({s.url: FEED})).collect()
    assert [c.title for c in got] == ["HydraFusion updates"]  # Copilot — только в описании
