"""Решение 48: changelog OpenAI, YouTube API, копия из Архива интернета, книги по фильтру, два сбора в день,
дневной лимит рубрики, подборка «Что посмотреть и послушать»."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import yaml

from vibe_stack import collect, prompts
from vibe_stack.board import LocalBoard
from vibe_stack.config import SourceConfig
from vibe_stack.fetch import FixtureFetcher, HttpFetcher
from vibe_stack.footer import allowed_hashtags
from vibe_stack.llm import FakeLLM
from vibe_stack.planner import pick_next
from vibe_stack.runtime import Runtime
from vibe_stack.sources import md_changelog, rss, youtube
from vibe_stack.storage import State
from vibe_stack.telegram import DryRunTelegram, Notifier
from vibe_stack.timeutil import local_date
from vibe_stack.watchlist import build, headline, remember_headline, run_watchlist

from .conftest import ROOT
from .test_planner_publish import ENABLED, post, published

CHANGELOG = """# Changelog

> The latest features and updates to the OpenAI API.

## October, 2026

### Oct 7

Update · Model: chat-latest

Updated the **chat-latest** snapshot. Read more [here](https://developers.openai.com/api/docs/models/chat-latest).

### Oct 6

Feature · Model: gpt-6-luna · API: v1/decisions

Released the [Decisions API](https://developers.openai.com/api/docs/guides/decisions) in beta. Fast.

### Oct 6

Update

Simplified API usage tiers from five to three.

## September, 2026

### Sep 22

Feature · Model: gpt-6-sol

Released GPT-6 Sol (`gpt-6-sol`).
"""
PAGE = "https://developers.openai.com/api/docs/changelog"


def client(routes: dict[str, object], seen: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        val = routes.get(str(req.url).split("?")[0] if str(req.url).split("?")[0] in routes else str(req.url))
        if isinstance(val, int):
            return httpx.Response(val, text="")
        if isinstance(val, dict):
            return httpx.Response(200, json=val)
        if isinstance(val, str):
            return httpx.Response(200, text=val, headers={"content-type": "text/html"})
        return httpx.Response(404, text="")

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- changelog в Markdown ---------------------------------------------------------------------------
def test_parse_changelog_entries() -> None:
    entries = md_changelog.parse_entries(CHANGELOG)
    assert [e.day.strftime("%m-%d") for e in entries] == ["10-07", "10-06", "10-06", "09-22"]
    assert entries[0].day == datetime(2026, 10, 7, 23, 59, tzinfo=UTC)  # дата без времени — конец дня
    assert entries[1].title == "Released the Decisions API in beta."  # первое предложение, без разметки ссылок
    assert len({e.id for e in entries}) == 4 and entries[1].id.startswith("2026-10-06-feature-model-gpt-6-luna")
    assert md_changelog.parse_entries(CHANGELOG)[1].id == entries[1].id  # id стабилен между запусками


def test_changelog_source_and_fetch_the_same_entry(cfg, now) -> None:
    routes = {PAGE + ".md": CHANGELOG}
    src = md_changelog.MarkdownChangelogSource(
        SourceConfig(name="oai", type="md_changelog", url=PAGE, title_prefix="OpenAI API: ", whitelist=True,
                     official_domains=["openai.com"]), client(routes))
    cands = src.collect()
    c = cands[1]
    assert c.url.startswith(PAGE + "?entry=2026-10-06-")
    assert c.title == "OpenAI API: Released the Decisions API in beta."
    assert len({x.canonical for x in cands}) == 4  # у каждой записи свой адрес после канонизации
    doc = HttpFetcher(cfg.fetch, lambda: now, client(routes), check_urls=False).fetch(c.url, purpose="verify")
    assert doc.ok and "Decisions API" in doc.text and "chat-latest" not in doc.text
    assert doc.published_meta == "2026-10-06T23:59:00+00:00"
    missing = HttpFetcher(cfg.fetch, lambda: now, client(routes), check_urls=False).fetch(
        PAGE + "?entry=nope", purpose="verify")
    assert not missing.ok


def test_changelog_backlog_is_limited_by_source_age(cfg) -> None:
    oai = next(s for s in cfg.sources if s.name == "openai-api-changelog")
    assert oai.max_age_days == 3 and "urgent" in oai.contours and oai.whitelist


# --- openai.com: копия из Архива интернета ---------------------------------------------------------------
def test_archive_fallback_for_blocked_official_site(cfg, now) -> None:
    url = "https://openai.com/index/gpt-6-for-everyone"
    page = ("<html><head><title>GPT-6</title></head><body><article>" + "GPT-6 is here. " * 60
            + "</article></body></html>")
    routes = {url: 403, f"https://web.archive.org/web/2id_/{url}": page}
    cfg.fetch.archive_fallback_hosts = ["openai.com"]
    doc = HttpFetcher(cfg.fetch, lambda: now, client(routes), check_urls=False).fetch(url, purpose="score")
    assert doc.ok and "web.archive.org" in doc.text and "GPT-6 is here" in doc.text
    cfg.fetch.archive_fallback_hosts = []
    assert not HttpFetcher(cfg.fetch, lambda: now, client(routes), check_urls=False).fetch(url, purpose="score").ok


# --- YouTube ---------------------------------------------------------------------------
UPLOADS = {"items": [
    {"snippet": {"title": "Building agents — talk", "description": "Chapters: 00:00 intro", "channelTitle": "AI Eng"},
     "contentDetails": {"videoId": "abcdefghijk", "videoPublishedAt": "2026-10-06T10:00:00Z"}},
    {"snippet": {"title": "quick tip #shorts"}, "contentDetails": {"videoId": "zzzzzzzzzzz"}},
    {"snippet": {"title": "Teaser"}, "contentDetails": {"videoId": "ttttttttttt"}},
]}
VIDEO = {"items": [{"id": "abcdefghijk",
                    "snippet": {"title": "Building agents — talk", "channelTitle": "AI Eng",
                                "publishedAt": "2026-10-06T10:00:00Z", "description": "00:00 intro\n05:10 evals",
                                "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/abcdefghijk/hq.jpg"}}},
                    "contentDetails": {"duration": "PT1H2M40S"}},
                   {"id": "ttttttttttt", "snippet": {"title": "Teaser"}, "contentDetails": {"duration": "PT1M5S"}}]}


def test_youtube_without_key_is_silent(monkeypatch) -> None:
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    src = youtube.YouTubeSource(SourceConfig(name="yt", type="youtube", channels=["UCx"]), client({}))
    assert src.collect() == []


def test_youtube_source_and_video_doc_key_in_header(cfg, now, monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key-123")
    seen: list[httpx.Request] = []
    routes = {f"{youtube.API}/playlistItems": UPLOADS, f"{youtube.API}/videos": VIDEO}
    src = youtube.YouTubeSource(SourceConfig(name="yt", type="youtube", channels=["UCabc"], min_minutes=8,
                                             skip_title_regex=["#shorts"]), client(routes, seen))
    (c,) = src.collect()  # #shorts — по заголовку, минутный тизер — по длительности
    assert c.url == "https://www.youtube.com/watch?v=abcdefghijk" and c.source_type == "youtube"
    assert c.summary.startswith("Длительность: 63 мин")
    assert "playlistId=UUabc" in str(seen[0].url)  # плейлист загрузок канала
    doc = HttpFetcher(cfg.fetch, lambda: now, client(routes, seen), check_urls=False).fetch(c.url, purpose="score")
    assert doc.ok and "Длительность: 63 мин" in doc.text and "05:10 evals" in doc.text
    assert doc.image == "https://i.ytimg.com/vi/abcdefghijk/hq.jpg"
    assert all("test-key-123" not in str(r.url) and r.headers["X-Goog-Api-Key"] == "test-key-123" for r in seen)


def test_book_video_needs_high_usefulness(cfg) -> None:
    """«Книга/видео» — только то, что стоит часа (решение 49): общий порог суммы мало, нужна польза 4 из 5."""
    from vibe_stack.models import ScoreResult
    from vibe_stack.steps import gate

    def score(category: str, usefulness: int) -> ScoreResult:
        return ScoreResult.model_validate({"id": "x", "category": category, "scores": {
            "novelty": 3, "usefulness": usefulness, "verifiability": 3, "substance": 2, "audience_fit": 2}})

    rubrics = {k: v.model_copy(update={"enabled": True}) for k, v in cfg.rubrics.items()}
    assert gate(score("book_video", 3), cfg, rubrics).reasons == ["low_usefulness"]  # сумма 13 из 15 — мало
    assert gate(score("book_video", 4), cfg, rubrics).passed
    assert gate(score("tool", 3), cfg, rubrics).passed  # у других рубрик порог прежний


def test_duration_and_video_id() -> None:
    assert youtube.duration_minutes("PT45M") == 45 and youtube.duration_minutes("PT1H0M31S") == 61
    assert youtube.video_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert youtube.video_id("https://example.com/watch?v=abcdefghijk") is None


# --- книги: только по теме ---------------------------------------------------------------------------
FEED = """<?xml version="1.0"?><rss><channel>
<item><title>Build AI Agents with Python</title><link>https://pragprog.com/titles/a/</link></item>
<item><title>Rust Brain Teasers</title><link>https://pragprog.com/titles/b/</link></item>
</channel></rss>"""


def test_rss_include_title_regex(cfg) -> None:
    books = next(s for s in cfg.sources if s.name == "pragprog-books")
    got = rss.RSSSource(books, client({books.url: FEED})).collect()
    assert [c.title for c in got] == ["Build AI Agents with Python"]


REDDIT = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>llama : add a GPU cache for MoE experts</title><link href="https://www.reddit.com/r/LocalLLaMA/comments/1/x/"/>
<content type="html">&lt;a href="https://github.com/ggml-org/llama.cpp/pull/1"&gt;[link]&lt;/a&gt;</content></entry>
<entry><title>I found a planet</title><link href="https://www.reddit.com/r/ClaudeAI/comments/2/y/"/>
<content type="html">&lt;a href="https://www.reddit.com/r/ClaudeAI/comments/2/y/"&gt;[link]&lt;/a&gt;</content></entry>
<entry><title>Benchmark chart</title><link href="https://www.reddit.com/r/ClaudeAI/comments/3/z/"/>
<content type="html">&lt;a href="https://i.redd.it/abc.png"&gt;[link]&lt;/a&gt;</content></entry>
</feed>"""


def test_reddit_takes_only_external_links(cfg) -> None:
    """Обсуждения и картинки Reddit роботам не открыть — берём только посты со ссылкой наружу (решение 48)."""
    src = next(s for s in cfg.sources if s.name == "reddit")
    got = rss.RSSSource(src, client({src.url: REDDIT})).collect()
    assert [(c.url, c.title) for c in got] == [("https://github.com/ggml-org/llama.cpp/pull/1",
                                                "llama : add a GPU cache for MoE experts")]


# --- два сбора в день ---------------------------------------------------------------------------
def make_rt(cfg, tmp_path, now, mode="publish") -> Runtime:
    state = State(tmp_path / "s.db")
    clock = lambda: now  # noqa: E731
    return Runtime(cfg=cfg, state=state, board=LocalBoard(tmp_path / "b.json"), tg=DryRunTelegram(tmp_path / "tg"),
                   notifier=Notifier(None, None, tmp_path / "a.log"),
                   llm=FakeLLM(cfg.llm, state, "p", clock, "Europe/Moscow", {}), fetcher=FixtureFetcher({}, clock),
                   clock=clock, run_id="p", out_dir=tmp_path, mode=mode, channel_id="@vibestack_off",
                   sources_factory=lambda _: [])


def test_morning_and_afternoon_collects_glossary_only_in_the_morning(cfg, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(collect, "_glossary_step", lambda rt, s: calls.append(collect.collect_half(rt)) or False)
    morning = datetime(2026, 10, 8, 4, 0, tzinfo=UTC)  # 07:00 МСК
    rt = make_rt(cfg, tmp_path, morning)
    assert collect.run_collect(rt)["half"] == "am"
    rt.mark_done(f"collect:{rt.today().isoformat()}:am")
    assert collect.run_collect(rt)["status"] == "already_ran_today"  # запасной утренний запуск
    rt.clock = lambda: morning + timedelta(hours=7)  # 14:00 МСК
    s = collect.run_collect(rt)
    assert s["half"] == "pm" and s.get("status") != "already_ran_today"
    assert calls == ["am"]  # «Слово дня» — только утром


def test_daily_max_per_rubric(cfg, now) -> None:
    cfg.planner.daily_max = {"book_video": 1}
    hist = [published("Talk A", "book_video", "https://a.dev/1", now - timedelta(hours=2))]
    q = [post("Talk B", "book_video", "https://b.dev/2", score=15, now=now)]
    res = pick_next(q, hist, today=local_date(now, "Europe/Moscow"), now=now, cfg=cfg.planner, rubric_enabled=ENABLED,
                    soft_rubric_repeat=True)
    assert res.post is None and "дневной максимум рубрики" in str(res.skipped)


def test_afternoon_plan_message_only_when_something_was_added(cfg, tmp_path, now) -> None:
    from vibe_stack import dayplan

    rt = make_rt(cfg, tmp_path, now)
    assert dayplan.plan_and_report(rt, update=True)["planned"] == 0 and rt.notifier.sent == []


# --- что посмотреть, послушать ---------------------------------------------------------------------------
def test_book_video_notes_allow_podcast_hashtag(cfg) -> None:
    out = prompts.write_prompt(["c"], {"category": "book_video", "url": "u", "title": "t", "mode": "standard"},
                               "book_video", "📚", "#книга", notes=prompts.rubric_notes("book_video"))
    assert "<что_посмотреть>" in out and "#подкаст" in out
    tags = allowed_hashtags(cfg.rubrics["book_video"], prompts.footer_template("book_video", "book_video"))
    assert {"#книга", "#видео", "#подкаст"} <= set(tags)
    assert "<рубрики_владельца>" in prompts.score_prompt({"id": "x"}, "doc")


def published_video(rt, mid: int, title: str, at: datetime, html: str) -> None:
    rt.state.record_published(ref=f"r{mid}", rubric="book_video", urgent=False, title=title, source_url="https://x.dev",
                              domain="x.dev", published_at=at, day=local_date(at, rt.tz), slot=None,
                              tg_message_id=mid, counts_regular=True)
    remember_headline(rt, mid, html)


def test_watchlist_collects_the_week(cfg, tmp_path) -> None:
    sunday = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)  # вс 12:00 МСК
    rt = make_rt(cfg, tmp_path, sunday)
    assert headline("📚 <b>AI Engineering</b> — Chip Huyen, книга, 45 мин\n\nО чём…") == \
        "AI Engineering — Chip Huyen, книга, 45 мин"
    published_video(rt, 41, "Talk", sunday - timedelta(days=2), "📚 <b>Доклад про агентов</b> — AI Engineer, видео")
    assert run_watchlist(rt)["status"] == "too_few"
    rt.state.db.execute("DELETE FROM kv WHERE key LIKE 'done:watchlist%'")
    published_video(rt, 45, "Pod", sunday - timedelta(days=1), "📚 <b>Latent Space: evals</b> — подкаст, 62 мин")
    published_video(rt, 30, "Old", sunday - timedelta(days=9), "📚 <b>Старое</b> — видео")  # прошлая неделя
    text, n = build(rt)
    assert n == 2 and "https://t.me/vibestack_off/41" in text and "62 мин" in text and "Старое" not in text
    s = run_watchlist(rt)
    assert s["status"] == "published" and rt.tg.sent[-1][1] == text
    assert run_watchlist(rt)["status"] == "already_published_this_week"


# --- workflow ---------------------------------------------------------------------------
def wf(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def test_workflows_wiring() -> None:
    collect_wf = wf("collect.yml")
    crons = [c["cron"] for c in collect_wf[True]["schedule"]]
    assert "0 11 * * *" in crons and "0 4 * * *" in crons  # 14:00 и 07:00 МСК
    # песочница запускается по имени workflow сбора — имена должны совпадать
    assert wf("sandbox.yml")[True]["workflow_run"]["workflows"] == [collect_wf["name"]]
    weekly = wf("weekly.yml")
    assert "watchlist" in weekly["jobs"]["run"]["with"]["commands"]
    assert "0 9 * * 0" in [c["cron"] for c in weekly[True]["schedule"]]
    run = json.dumps(wf("_run.yml"))
    assert "watchlist)" in run and "YOUTUBE_API_KEY" in run


def test_all_configured_sources_build(cfg, now) -> None:
    from vibe_stack.sources import build_source

    for s in cfg.sources:
        assert build_source(s, client({}), lambda: now, 30).name == s.name
    assert len(cfg.sources) >= 20


@pytest.mark.parametrize("name", ["reddit", "latent-space", "youtube", "pragprog-books", "cursor-changelog"])
def test_new_sources_present(cfg, name) -> None:
    assert any(s.name == name for s in cfg.sources)
