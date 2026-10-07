"""Адаптеры источников и загрузка первоисточника — на MockTransport, без сети."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from vibe_stack.config import SourceConfig
from vibe_stack.fetch import HttpFetcher, UnsafeURL, check_public_url
from vibe_stack.sources import collect_all
from vibe_stack.sources.github import GitHubReleasesSource, GitHubSearchSource
from vibe_stack.sources.hackernews import HackerNewsSource
from vibe_stack.sources.rss import RSSSource
from vibe_stack.sources.sitemap import SitemapSource

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Introducing X</title><link>https://openai.com/index/x</link>
<description><![CDATA[<p>New <b>model</b></p>]]></description>
<pubDate>Wed, 07 Oct 2026 05:00:00 GMT</pubDate></item></channel></rss>"""

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.anthropic.com/news/new-thing</loc><lastmod>2026-10-06T19:39:32.000Z</lastmod></url>
<url><loc>https://www.anthropic.com/news/old-thing</loc><lastmod>2025-01-01T00:00:00.000Z</lastmod></url>
<url><loc>https://www.anthropic.com/careers</loc><lastmod>2026-10-06T00:00:00.000Z</lastmod></url>
<url><loc>https://www.anthropic.com/news</loc><lastmod>2026-10-06T00:00:00.000Z</lastmod></url>
</urlset>"""


def client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        for prefix, resp in routes.items():
            if str(req.url).startswith(prefix):
                return resp
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_rss(now) -> None:
    cfg = SourceConfig(name="openai-news", type="rss", url="https://openai.com/news/rss.xml", whitelist=True,
                       official_domains=["openai.com"])
    got = RSSSource(cfg, client({"https://openai.com/news/rss.xml": httpx.Response(200, text=RSS)})).collect()
    assert len(got) == 1
    c = got[0]
    assert c.title == "Introducing X" and c.summary == "New model" and c.whitelist
    assert c.published_at.isoformat() == "2026-10-07T05:00:00+00:00"


def test_rss_skip_title_regex() -> None:
    feed = RSS.replace("Introducing X", "Quoting Someone")
    cfg = SourceConfig(name="sw", type="rss", url="https://sw.net/atom", skip_title_regex=["^Quoting "])
    assert RSSSource(cfg, client({"https://sw.net/atom": httpx.Response(200, text=feed)})).collect() == []


def test_sitemap_new_news_only(now) -> None:
    cfg = SourceConfig(name="anthropic-news", type="sitemap", url="https://www.anthropic.com/sitemap.xml",
                       path_prefix="/news/")
    src = SitemapSource(cfg, client({"https://www.anthropic.com/sitemap.xml": httpx.Response(200, text=SITEMAP)}),
                        since=now - timedelta(days=30))
    got = src.collect()
    assert [c.url for c in got] == ["https://www.anthropic.com/news/new-thing"]
    assert got[0].title == "new thing"


def test_github_releases_skip_prerelease() -> None:
    rels = [
        {"html_url": "https://github.com/o/r/releases/tag/v2.0.0-alpha", "tag_name": "v2.0.0-alpha",
         "prerelease": True, "draft": False, "published_at": "2026-10-06T00:00:00Z"},
        {"html_url": "https://github.com/o/r/releases/tag/v1.9.0", "tag_name": "v1.9.0", "name": "1.9.0",
         "prerelease": False, "draft": False, "published_at": "2026-10-05T00:00:00Z", "body": "notes"},
    ]
    cfg = SourceConfig(name="rel", type="github_releases", repo="o/r")
    got = GitHubReleasesSource(cfg, client({"https://api.github.com/repos/o/r/releases":
                                            httpx.Response(200, json=rels)})).collect()
    assert [c.extra["tag"] for c in got] == ["v1.9.0"]  # только последний стабильный


def test_github_search_dedups_across_queries(now) -> None:
    item = {"full_name": "a/b", "html_url": "https://github.com/a/b", "description": "d",
            "created_at": "2026-10-01T00:00:00Z", "pushed_at": "2026-10-06T00:00:00Z", "stargazers_count": 99,
            "topics": ["mcp"]}
    cfg = SourceConfig(name="gh", type="github_search", queries=["topic:mcp", "topic:mcp-server"])
    got = GitHubSearchSource(cfg, client({"https://api.github.com/search/repositories":
                                          httpx.Response(200, json={"items": [item]})}), now).collect()
    assert len(got) == 1 and got[0].extra["stars"] == 99


def test_hackernews_uses_story_url_and_skips_ask_hn(now) -> None:
    hits = {"hits": [
        {"title": "Show HN: tool", "url": "https://github.com/a/tool", "points": 120,
         "created_at": "2026-10-06T10:00:00Z", "objectID": "1"},
        {"title": "Ask HN: what do you use?", "url": None, "points": 300, "created_at": "2026-10-06T10:00:00Z",
         "objectID": "2"},
    ]}
    cfg = SourceConfig(name="hn", type="hackernews", queries=["Claude Code"], min_points=60)
    got = HackerNewsSource(cfg, client({"https://hn.algolia.com/": httpx.Response(200, json=hits)}), now).collect()
    assert [c.url for c in got] == ["https://github.com/a/tool"]


def test_failing_source_does_not_break_others(now) -> None:
    good = SourceConfig(name="ok", type="rss", url="https://ok.dev/feed")
    bad = SourceConfig(name="bad", type="rss", url="https://bad.dev/feed")
    c = client({"https://ok.dev/feed": httpx.Response(200, text=RSS), "https://bad.dev/feed": httpx.Response(500)})
    got, errors = collect_all([RSSSource(bad, c), RSSSource(good, c)])
    assert len(got) == 1 and "bad" in errors


def test_fetch_generic_html(cfg, now) -> None:
    html = ("<html><head><title>Post</title><meta property='article:published_time' content='2026-10-06'>"
            "</head><body><nav>menu</nav><article>" + "<p>Real content here.</p>" * 40 + "</article>"
            "<script>evil()</script></body></html>")
    f = HttpFetcher(cfg.fetch, lambda: now, client=client({"https://blog.dev/p": httpx.Response(
        200, text=html, headers={"content-type": "text/html; charset=utf-8"})}), check_urls=False)
    doc = f.fetch("https://blog.dev/p", purpose="score")
    assert doc.ok and doc.published_meta == "2026-10-06"
    assert "Real content here." in doc.text and "menu" not in doc.text and "evil" not in doc.text


def test_fetch_github_repo_readme(cfg, now) -> None:
    routes = {
        "https://api.github.com/repos/a/b/readme": httpx.Response(200, text="# b\nREADME body"),
        "https://api.github.com/repos/a/b/releases/latest": httpx.Response(404),
        "https://api.github.com/repos/a/b": httpx.Response(200, json={
            "full_name": "a/b", "description": "desc", "stargazers_count": 5, "html_url": "https://github.com/a/b",
            "created_at": "2026-10-01T00:00:00Z", "pushed_at": "2026-10-06T00:00:00Z", "topics": []}),
    }
    f = HttpFetcher(cfg.fetch, lambda: now, client=client(routes), check_urls=False)
    doc = f.fetch("https://github.com/a/b", purpose="score")
    assert doc.ok and "README body" in doc.text and "desc" in doc.text


def test_fetch_truncates_and_handles_errors(cfg, now) -> None:
    cfg.fetch.max_doc_chars = 200
    f = HttpFetcher(cfg.fetch, lambda: now, client=client({
        "https://big.dev": httpx.Response(200, text="line\n" * 1000, headers={"content-type": "text/plain"}),
        "https://pdf.dev": httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
    }), check_urls=False)
    assert len(f.fetch("https://big.dev", purpose="score").text) <= 220
    assert not f.fetch("https://pdf.dev", purpose="score").ok
    assert not f.fetch("https://missing.dev", purpose="score").ok


def test_ssrf_guard() -> None:
    for bad in ("http://localhost/x", "http://127.0.0.1/x", "http://169.254.169.254/latest", "file:///etc/passwd",
                "http://10.0.0.5/"):
        with pytest.raises(UnsafeURL):
            check_public_url(bad)
