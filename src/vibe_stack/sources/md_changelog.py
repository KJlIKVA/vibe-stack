"""Changelog в Markdown (решение 48): каждая запись — отдельный кандидат.

Нужен для OpenAI: страницы openai.com отвечают роботам из GitHub Actions 403, а официальный changelog API
(developers.openai.com/api/docs/changelog.md) открыт. Формат:

    ## October, 2026
    ### Oct 7
    Update · Model: chat-latest
    Текст записи…

У записи нет своего адреса, поэтому адрес кандидата — страница changelog с параметром ?entry=<id>
(параметр переживает канонизацию URL, якорь — нет). Тот же разбор использует загрузчик (fetch.py):
документ для проверки — текст именно этой записи.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from ..config import SourceConfig
from ..models import Candidate

_MONTH_RE = re.compile(r"^##\s+([A-Za-z]+),?\s+(\d{4})\s*$")
_DAY_RE = re.compile(r"^###\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\s*$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


@dataclass
class Entry:
    id: str
    day: datetime   # дата без времени — считаем концом дня UTC, чтобы запись дня не «устарела» раньше срока
    label: str      # «Feature · Model: gpt-6-luna · API: v1/decisions»
    body: str       # текст записи (Markdown)

    @property
    def title(self) -> str:
        first = re.split(r"(?<=[.!?])\s", _LINK_RE.sub(r"\1", self.body).replace("**", ""), maxsplit=1)[0]
        return first.strip()[:160] or self.label

    def text(self) -> str:
        return f"Дата записи: {self.day:%Y-%m-%d}\n{self.label}\n\n{self.body}".strip()


def _slug(text: str, words: int = 8) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())[:words])[:80]


def parse_entries(md: str) -> list[Entry]:
    out: list[Entry] = []
    year = month = 0
    cur: dict[str, object] | None = None

    def close() -> None:
        if cur is None:
            return
        lines = [ln for ln in cur["lines"] if ln.strip()]  # type: ignore[attr-defined]
        if not lines:
            return
        label, body = lines[0].strip(), "\n".join(lines[1:]).strip()
        day = cur["day"]
        assert isinstance(day, datetime)
        base = f"{day:%Y-%m-%d}-{_slug(label)}-{_slug(body, 5)}"
        eid, n = base, 2
        while any(e.id == eid for e in out):
            eid, n = f"{base}-{n}", n + 1
        out.append(Entry(id=eid, day=day, label=label, body=body))

    for line in md.splitlines():
        if m := _MONTH_RE.match(line):
            close()
            cur = None
            month, year = _MONTHS.get(m.group(1)[:3].lower(), 0), int(m.group(2))
        elif (m := _DAY_RE.match(line)) and year:
            close()
            mon = _MONTHS.get(m.group(1).lower(), month)
            cur = {"day": datetime(year, mon, int(m.group(2)), 23, 59, tzinfo=UTC), "lines": []}
        elif cur is not None:
            cur["lines"].append(line)  # type: ignore[attr-defined]
    close()
    return out


def entry_id(url: str) -> str | None:
    return (parse_qs(urlsplit(url).query).get("entry") or [None])[0]


def md_url(page_url: str) -> str:
    """Адрес Markdown-версии страницы: …/changelog → …/changelog.md."""
    parts = urlsplit(page_url)
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}.md"


class MarkdownChangelogSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def collect(self) -> list[Candidate]:
        assert self.cfg.url
        r = self.client.get(md_url(self.cfg.url), follow_redirects=True)
        r.raise_for_status()
        page = self.cfg.url.split("?")[0].rstrip("/")
        return [Candidate(
            source=self.name,
            source_type="md_changelog",
            url=f"{page}?entry={e.id}",
            title=f"{self.cfg.title_prefix}{e.title}" if self.cfg.title_prefix else e.title,
            summary=e.text()[:1500],
            published_at=e.day,
            whitelist=self.cfg.whitelist,
            official_domains=self.cfg.official_domains,
            signal=1.0,
        ) for e in parse_entries(r.text)[:40]]
