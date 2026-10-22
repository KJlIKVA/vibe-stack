"""sitemap.xml как список новых страниц (для сайтов без RSS). Новая страница = URL, которого не было раньше.

lastmod — дата изменения, а не публикации, поэтому дата публикации проверяется уже по самой странице.

У claude.com в sitemap нет lastmod вовсе (решение 70: там выходят Claude Duet и другие продукты). Тогда датой
страницы служит момент, когда её адрес впервые появился в sitemap: адреса хранятся в состоянии (kv). При первом
опросе источника все адреса — архив, кандидатами становятся только появившиеся позже.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import SourceConfig
from ..models import Candidate
from ..timeutil import Clock, iso, parse_dt, utc_now

ARCHIVE = datetime(1970, 1, 1, tzinfo=UTC)  # адрес был в sitemap уже при первом опросе


class KV(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, value: str) -> None: ...

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class FirstSeen:
    """Когда адрес впервые появился в списке источника без дат (решение 70): sitemap claude.com, страница блога Meta.
    При первом опросе всё, что уже есть, — архив; адреса из first_poll_new — новые, даже если уже стали архивом
    (так возвращаются свежие статьи, которые первый опрос принял за архив)."""

    def __init__(self, state: KV, name: str, clock: Clock, first_poll_new: list[str]) -> None:
        self.state = state
        self.key = f"sitemap_first_seen:{name}"
        raw = state.get(self.key)
        self.saved: dict[str, str] | None = json.loads(raw) if raw else None
        self.known = dict(self.saved or {})
        self.clock = clock
        self.fresh = set(first_poll_new)

    def date(self, url: str) -> datetime | None:
        archived = iso(ARCHIVE)
        if url not in self.known or (self.known[url] == archived and url in self.fresh):
            archive = self.saved is None and url not in self.fresh
            self.known[url] = (archived if archive else iso(self.clock())) or ""
        return parse_dt(self.known[url])

    def save(self) -> None:
        if self.known != (self.saved or {}):
            self.state.put(self.key, json.dumps(self.known, ensure_ascii=False))


class SitemapSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client, since: datetime, state: KV | None = None,
                 clock: Clock = utc_now) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client
        self.since = since
        self.state = state  # без состояния страницы без lastmod пропускаются
        self.clock = clock

    def collect(self) -> list[Candidate]:
        assert self.cfg.url
        r = self.client.get(self.cfg.url, follow_redirects=True)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        seen = FirstSeen(self.state, self.name, self.clock, self.cfg.first_poll_new) if self.state else None
        # решение 70: у Cerebral Valley тысячи событий — берём только адреса, подходящие под include_text_regex
        include = [re.compile(p, re.IGNORECASE) for p in self.cfg.include_text_regex]
        out = []
        for node in root.findall("sm:url", NS):
            loc = (node.findtext("sm:loc", default="", namespaces=NS) or "").strip()
            lastmod = parse_dt(node.findtext("sm:lastmod", default="", namespaces=NS))
            path = urlsplit(loc).path
            prefix = self.cfg.path_prefix or "/"
            if not loc or not path.startswith(prefix) or path.rstrip("/") == prefix.rstrip("/"):
                continue
            if include and not any(p.search(path) for p in include):
                continue
            if lastmod is None and seen is not None:
                lastmod = seen.date(loc)
            if lastmod is None or lastmod < self.since:
                continue
            out.append(page_candidate(self.cfg, loc, lastmod, "sitemap"))
        if seen is not None:
            seen.save()
        return out


def page_candidate(cfg: SourceConfig, url: str, when: datetime, source_type: str) -> Candidate:
    """Страница из списка: заголовок — пока slug, настоящий код берёт со страницы (with_page_title)."""
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return Candidate(
        source=cfg.name,
        source_type=source_type,
        url=url,
        title=slug.replace("-", " "),
        updated_at=when,
        whitelist=cfg.whitelist,
        official_domains=cfg.official_domains,
        # свежие страницы первыми: в sitemap сотни старых страниц с недавним lastmod
        signal=1.0 + when.timestamp() / 1e11,
    )


_HREF = re.compile(r'href="([^"#?]+)"')


class PageLinksSource:
    """Страница-список без RSS и дат в sitemap (блог Meta, решение 70): ссылки с path_prefix на том же сайте —
    статьи; новая ссылка — кандидат, её дата — первое появление в списке (FirstSeen)."""

    def __init__(self, cfg: SourceConfig, client: httpx.Client, since: datetime, state: KV | None = None,
                 clock: Clock = utc_now) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client
        self.since = since
        self.state = state
        self.clock = clock

    def collect(self) -> list[Candidate]:
        assert self.cfg.url and self.cfg.path_prefix
        if self.state is None:
            return []  # без памяти о прошлых опросах любая ссылка выглядела бы новой
        r = self.client.get(self.cfg.url, follow_redirects=True)
        r.raise_for_status()
        host = urlsplit(self.cfg.url).hostname
        prefix = self.cfg.path_prefix.rstrip("/") + "/"
        seen = FirstSeen(self.state, self.name, self.clock, self.cfg.first_poll_new)
        out = []
        for href in dict.fromkeys(_HREF.findall(r.text)):
            url = urljoin(self.cfg.url, href).rstrip("/")
            parts = urlsplit(url)
            if parts.hostname != host or not parts.path.startswith(prefix):
                continue
            when = seen.date(url)
            if when is not None and when >= self.since:
                out.append(page_candidate(self.cfg, url, when, "page_links"))
        seen.save()
        return out
