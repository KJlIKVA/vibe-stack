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


@pytest.mark.parametrize(("queued", "allowed"), [(4, False), (5, True), (8, True)])
def test_backlog_allows_second_post_a_day(cfg, now, queued, allowed) -> None:
    """Очередь «что посмотреть» от 5 (вместе с вышедшим сегодня) — второй пост в день (решение 50)."""
    cfg.planner.daily_max, cfg.planner.backlog_daily_max, cfg.planner.backlog_queue = {"book_video": 1}, \
        {"book_video": 2}, 5
    hist = [published("Talk A", "book_video", "https://a.dev/1", now - timedelta(hours=2))]
    q = [post(f"Video {i} unique{i}", "book_video", f"https://v{i}.dev/", score=15, now=now)
         for i in range(queued - 1)]
    res = pick_next(q, hist, today=local_date(now, "Europe/Moscow"), now=now, cfg=cfg.planner, rubric_enabled=ENABLED,
                    soft_rubric_repeat=True)
    assert (res.post is not None) is allowed
    hist.append(published("Talk B", "book_video", "https://b.dev/2", now - timedelta(hours=1)))
    res = pick_next(q, hist, today=local_date(now, "Europe/Moscow"), now=now, cfg=cfg.planner, rubric_enabled=ENABLED,
                    soft_rubric_repeat=True)
    assert res.post is None  # третий — нет: при любой очереди не больше двух


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


def test_book_post_knows_today_and_gets_larger_cover(cfg, tmp_path, now) -> None:
    """Дата выхода книги — относительно сегодняшней; обложка Manning — крупная копия (решение 50)."""
    from vibe_stack import images
    from vibe_stack.models import Candidate
    from vibe_stack.steps import write

    rt = make_rt(cfg, tmp_path, now, mode="dry-run")
    c = Candidate(source="manning-books", source_type="jsonld_list", url="https://www.manning.com/books/x", title="X")
    rt.llm.responses[("write", c.id)] = "📚 <b>Book</b> — книга.\n\nО чём.\n<b>Где взять:</b> по ссылке"
    write(rt, c, "book_video", cfg.rubrics["book_video"], ["a", "b"], mode="standard")
    prompt = next(p for step, ctx, p in rt.llm.calls if step == "write")
    assert f"Сегодня {rt.today():%d.%m.%Y}." in prompt and "{{сегодня}}" not in prompt
    assert images.larger("https://images.manning.com/360/480/resize/book/a/b/DOTD_x.png") == \
        "https://images.manning.com/720/960/resize/book/a/b/DOTD_x.png"
    assert images.larger("https://media.springernature.com/w153/springer-static/cover/book/1.jpg") == \
        "https://media.springernature.com/w306/springer-static/cover/book/1.jpg"


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


@pytest.mark.parametrize("name", ["reddit", "latent-space", "youtube", "pragprog-books", "cursor-changelog",
                                  "manning-books", "nostarch-books"])
def test_new_sources_present(cfg, name) -> None:
    assert any(s.name == name for s in cfg.sources)


# --- решение 50: тип материала, книги Manning, свежесть книг -------------------------------------------
PODCASTS = """<?xml version="1.0"?><rss><channel>
<item><title>Building evals with a practitioner</title><link>https://www.latent.space/p/evals</link>
<enclosure url="https://cdn.example/ep.mp3" type="audio/mpeg" length="1"/></item>
<item><title>AINews: not much happened</title><link>https://www.latent.space/p/ainews</link></item>
</channel></rss>"""

CATALOG = """<html><script type="application/ld+json">{"@context": "https://schema.org", "@type": "ItemList",
"itemListElement": [
 {"@type": "Product", "name": "Evaluating AI Systems", "url": "https://www.manning.com/books/evaluating-ai-systems"},
 {"@type": "Product", "name": "Rust in Action", "url": "https://www.manning.com/books/rust-in-action"}]}</script></html>"""


def test_podcast_is_an_audio_entry(cfg) -> None:
    src = next(s for s in cfg.sources if s.name == "latent-space")
    got = {c.title: c.extra.get("media") for c in rss.RSSSource(src, client({src.url: PODCASTS})).collect()}
    assert got == {"Building evals with a practitioner": "podcast", "AINews: not much happened": None}


def test_manning_catalog_books(cfg) -> None:
    from vibe_stack.sources import build_source, collect_all

    src = next(s for s in cfg.sources if s.name == "manning-books")
    got, errors = collect_all([build_source(src, client({src.url: CATALOG}), datetime.now, 30)])
    assert not errors and [(c.title, c.extra["media"]) for c in got] == [("Evaluating AI Systems", "book")]
    assert got[0].for_prompt()["extra"] == {"media": "book"}  # тип видит модель при оценке


def test_book_freshness_uses_source_limit(cfg, now) -> None:
    from vibe_stack.models import FetchedDoc
    from vibe_stack.steps import doc_guards

    doc = FetchedDoc(url="https://x", final_url="https://x", ok=True, fetched_at=now, text="книга",
                     published_meta=(now - timedelta(days=90)).isoformat())
    assert doc_guards(doc, cfg, now) == ["outdated"]  # новость старше 30 дней — устарела
    assert doc_guards(doc, cfg, now, max_age_days=180) == []  # книге 3 месяца — нет


# --- решение 52: книги — раз в неделю, видео и подкасты — раз в день; при большой очереди — чаще ------------
BOOK, VIDEO_POST = "📚 <b>Book</b>\n\nТекст.\n\n#книга", "📚 <b>Talk</b>\n\nТекст.\n\n#видео"


def test_media_group_by_hashtag() -> None:
    from vibe_stack.planner import media_group

    assert media_group(BOOK) == "book" and media_group(VIDEO_POST) == "watch"
    assert media_group("…\n\n#подкаст") == "watch" and media_group("…\n\n#инструмент") is None


def _plan(cfg, now, queue, hist, groups):
    return pick_next(queue, hist, today=local_date(now, "Europe/Moscow"), now=now, cfg=cfg.planner,
                     rubric_enabled=ENABLED, soft_rubric_repeat=True, groups=groups)


def test_book_weekly_and_video_daily_are_separate(cfg, now) -> None:
    hist = [published("Book A", "book_video", "https://a.dev/1", now - timedelta(hours=2))]
    groups = {"Book A": "book", "Book B unique": "book", "Talk C unique": "watch"}
    book = post("Book B unique", "book_video", "https://b.dev/2", score=15, now=now)
    talk = post("Talk C unique", "book_video", "https://c.dev/3", score=12, now=now)
    res = _plan(cfg, now, [book, talk], hist, groups)
    assert res.post is talk  # книга на этой неделе уже была, а видео сегодня — ещё нет
    assert "недельный максимум рубрики" in str(res.skipped["Book B unique"])


@pytest.mark.parametrize(("books_this_week", "queued", "allowed"), [(1, 3, False), (1, 4, True), (3, 9, False)])
def test_book_backlog_allows_more_per_week(cfg, now, books_this_week, queued, allowed) -> None:
    """Очередь книг от 5 (вместе с вышедшими за неделю) — до трёх в неделю, а не одна."""
    hist = [published(f"Alpha{i} volume", "book_video", f"https://h{i}.dev/", now - timedelta(hours=1 + i))
            for i in range(books_this_week)]  # вышли сегодня, в среду: та же неделя
    q = [post(f"Zeta{i} handbook{i}", "book_video", f"https://q{i}.dev/", score=15, now=now) for i in range(queued)]
    groups = {h.ref: "book" for h in hist} | {p.ref: "book" for p in q}
    cfg.planner.daily_max = {}  # проверяем только недельный лимит книг
    assert (_plan(cfg, now, q, hist, groups).post is not None) is allowed


def test_book_waits_in_queue_longer_than_news(cfg, tmp_path, now) -> None:
    from vibe_stack.models import PostRecord, Status
    from vibe_stack.publish import _drop_stale_and_published

    rt = make_rt(cfg, tmp_path, now)
    old = now - timedelta(days=10)
    book = PostRecord(title="Book", rubric="book_video", status=Status.APPROVED, source_url="https://m.dev/b",
                      found_at=old, html=BOOK)
    talk = PostRecord(title="Talk", rubric="book_video", status=Status.APPROVED, source_url="https://y.dev/t",
                      found_at=old, html=VIDEO_POST)
    assert _drop_stale_and_published(rt, [book, talk], now) == [book]


# --- решение 53: Apress через Open Library, страница книги — Springer по ISBN -----------------------------
def test_openlibrary_books_link_to_publisher_page(cfg) -> None:
    from vibe_stack.sources import build_source, collect_all
    from vibe_stack.sources.openlibrary import API

    src = next(s for s in cfg.sources if s.name == "apress-books")
    docs = {"docs": [
        {"title": "Mastering LangChain and LangGraph", "isbn": ["9798868829451", "x"], "first_publish_year": 2026,
         "author_name": ["Ankur Kulshreshtha"]},
        {"title": "Creating ChatGPT Apps", "isbn": ["9798868812200"], "first_publish_year": 2024},  # старая
        {"title": "No ISBN", "first_publish_year": 2026},
    ]}
    now = datetime(2026, 10, 8, tzinfo=UTC)
    got, errors = collect_all([build_source(src, client({API: docs}), lambda: now, 30)])
    assert not errors
    assert [(c.url, c.extra["media"]) for c in got] == [("https://link.springer.com/book/9798868829451", "book")]
