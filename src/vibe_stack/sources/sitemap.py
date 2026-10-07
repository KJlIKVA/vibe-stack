"""sitemap.xml как список новых страниц (для сайтов без RSS). Новая страница = URL, которого не было раньше.

lastmod — дата изменения, а не публикации, поэтому дата публикации проверяется уже по самой странице.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from ..config import SourceConfig
from ..models import Candidate
from ..timeutil import parse_dt

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class SitemapSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client, since: datetime) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client
        self.since = since

    def collect(self) -> list[Candidate]:
        assert self.cfg.url
        r = self.client.get(self.cfg.url, follow_redirects=True)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for node in root.findall("sm:url", NS):
            loc = (node.findtext("sm:loc", default="", namespaces=NS) or "").strip()
            lastmod = parse_dt(node.findtext("sm:lastmod", default="", namespaces=NS))
            path = urlsplit(loc).path
            prefix = self.cfg.path_prefix or "/"
            if not loc or not path.startswith(prefix) or path.rstrip("/") == prefix.rstrip("/"):
                continue
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
                signal=1.0,
            ))
        return out
