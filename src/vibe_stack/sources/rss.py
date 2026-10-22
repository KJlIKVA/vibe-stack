from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime

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


class RSSSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def entries(self) -> list[feedparser.FeedParserDict]:
        """Записи ленты; pages > 1 — ещё и следующие страницы (WordPress ?paged=N): у GitHub changelog в ленте 10
        записей, а за полдня между сборами их выходит больше (решение 70)."""
        assert self.cfg.url
        out: list[feedparser.FeedParserDict] = []
        for page in range(1, self.cfg.pages + 1):
            sep = "&" if "?" in self.cfg.url else "?"
            url = self.cfg.url if page == 1 else f"{self.cfg.url}{sep}paged={page}"
            r = self.client.get(url, follow_redirects=True)
            if page > 1 and r.status_code == 404:
                break
            r.raise_for_status()
            out += feedparser.parse(r.content).entries[:40]
        return out

    def collect(self) -> list[Candidate]:
        skip = [re.compile(p) for p in self.cfg.skip_title_regex]
        include = [re.compile(p, re.IGNORECASE) for p in self.cfg.include_title_regex]
        include_text = [re.compile(p, re.IGNORECASE) for p in self.cfg.include_text_regex]
        out = []
        links: set[str] = set()
        for e in self.entries():
            link = e.get("link")
            title = (e.get("title") or "").strip()
            if not link or link in links or not title or any(p.search(title) for p in skip):
                continue
            if include and not any(p.search(title) for p in include):
                continue
            summary_html = e.get("summary") or ""
            _, summary, _ = html_to_text(summary_html) if "<" in summary_html else ("", summary_html, {})
            if include_text and not any(p.search(f"{title}\n{summary}") for p in include_text):
                continue
            links.add(link)
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
                # выпуск подкаста — запись с аудио (у Latent Space в одной ленте и статьи, и выпуски)
                extra={"media": "podcast"} if any(str(x.get("type", "")).startswith("audio/")
                                                  for x in e.get("enclosures") or []) else {},
            ))
        return out
