from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import feedparser
import httpx

from ..config import SourceConfig
from ..htmltext import html_to_text
from ..models import Candidate


def _entry_dt(entry: feedparser.FeedParserDict, *names: str) -> datetime | None:
    for n in names:
        # keys(), а не get(): иначе feedparser подменяет отсутствующий updated_parsed на published_parsed
        t = entry[n] if n in entry.keys() else None  # noqa: SIM118
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=UTC)
    return None


_LINK_RE = re.compile(r'<a href="([^"]+)">\s*\[link\]\s*</a>')


def external_link(summary_html: str, hosts: list[str]) -> str | None:
    """Внешняя ссылка записи агрегатора («[link]» у Reddit) или None, если она ведёт на сам агрегатор."""
    m = _LINK_RE.search(summary_html)
    if not m:
        return None
    host = (urlsplit(m.group(1)).hostname or "").lower()
    if not host or any(host == h or host.endswith("." + h) for h in hosts):
        return None
    return m.group(1)


class RSSSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def collect(self) -> list[Candidate]:
        assert self.cfg.url
        r = self.client.get(self.cfg.url, follow_redirects=True)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        skip = [re.compile(p) for p in self.cfg.skip_title_regex]
        include = [re.compile(p, re.IGNORECASE) for p in self.cfg.include_title_regex]
        out = []
        for e in feed.entries[:40]:
            link = e.get("link")
            title = (e.get("title") or "").strip()
            if not link or not title or any(p.search(title) for p in skip):
                continue
            if include and not any(p.search(title) for p in include):
                continue
            summary_html = e.get("summary") or ""
            if self.cfg.aggregator_hosts:
                link = external_link(summary_html, self.cfg.aggregator_hosts)
                if not link:
                    continue
            _, summary, _ = html_to_text(summary_html) if "<" in summary_html else ("", summary_html, {})
            out.append(Candidate(
                source=self.name,
                source_type="rss",
                url=link,
                title=title,
                summary=summary[:1500],
                published_at=_entry_dt(e, "published_parsed", "updated_parsed"),
                updated_at=_entry_dt(e, "updated_parsed"),
                whitelist=self.cfg.whitelist,
                official_domains=self.cfg.official_domains,
                signal=1.0 if self.cfg.whitelist else 0.5,
            ))
        return out
