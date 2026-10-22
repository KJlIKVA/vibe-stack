"""sitemap.xml как список новых страниц (для сайтов без RSS). Новая страница = URL, которого не было раньше.

lastmod — дата изменения, а не публикации, поэтому дата публикации проверяется уже по самой странице.

У claude.com в sitemap нет lastmod вовсе (решение 70: там выходят Claude Duet и другие продукты). Тогда датой
страницы служит момент, когда её адрес впервые появился в sitemap: адреса хранятся в состоянии (kv). При первом
опросе источника все адреса — архив, кандидатами становятся только появившиеся позже.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from ..config import SourceConfig
from ..models import Candidate
from ..timeutil import Clock, iso, parse_dt, utc_now

ARCHIVE = datetime(1970, 1, 1, tzinfo=UTC)  # адрес был в sitemap уже при первом опросе


class KV(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, value: str) -> None: ...

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


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
        key = f"sitemap_first_seen:{self.name}"
        raw = self.state.get(key) if self.state else None
        first_seen: dict[str, str] | None = json.loads(raw) if raw else None
        known = dict(first_seen or {})
        out = []
        for node in root.findall("sm:url", NS):
            loc = (node.findtext("sm:loc", default="", namespaces=NS) or "").strip()
            lastmod = parse_dt(node.findtext("sm:lastmod", default="", namespaces=NS))
            path = urlsplit(loc).path
            prefix = self.cfg.path_prefix or "/"
            if not loc or not path.startswith(prefix) or path.rstrip("/") == prefix.rstrip("/"):
                continue
            if lastmod is None and self.state is not None:
                if loc not in known:
                    archive = first_seen is None and loc not in self.cfg.first_poll_new
                    known[loc] = iso(ARCHIVE if archive else self.clock()) or ""
                lastmod = parse_dt(known[loc])
            if lastmod is None or lastmod < self.since:
                continue
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            out.append(Candidate(
                source=self.name,
                source_type="sitemap",
                url=loc,
                title=slug.replace("-", " "),
                updated_at=lastmod,
                whitelist=self.cfg.whitelist,
                official_domains=self.cfg.official_domains,
                # свежие страницы первыми: в sitemap сотни старых страниц с недавним lastmod
                signal=1.0 + lastmod.timestamp() / 1e11,
            ))
        if self.state is not None and known != (first_seen or {}):
            self.state.put(key, json.dumps(known, ensure_ascii=False))
        return out
