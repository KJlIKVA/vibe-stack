from __future__ import annotations

import html
import math
import re
from datetime import datetime
from typing import Any

import httpx

from ..config import SourceConfig, env
from ..models import Candidate
from ..timeutil import parse_dt, utc_now

API = "https://api.github.com"


def gh_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token := env("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {token}"
    return h


class GitHubReleasesSource:
    """Последний стабильный релиз репозитория (REST API). Пре-релизы и черновики пропускаем;
    серия патчей подряд — это одна новость, поэтому берём только самый свежий."""

    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def collect(self) -> list[Candidate]:
        assert self.cfg.repo
        r = self.client.get(f"{API}/repos/{self.cfg.repo}/releases", params={"per_page": 10}, headers=gh_headers())
        r.raise_for_status()
        out = []
        for rel in r.json():
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel.get("tag_name") or ""
            out.append(Candidate(
                source=self.name,
                source_type="github_releases",
                url=rel["html_url"],
                title=f"{self.cfg.repo} {rel.get('name') or tag}".strip(),
                summary=(rel.get("body") or "")[:1500],
                published_at=parse_dt(rel.get("published_at")),
                whitelist=self.cfg.whitelist,
                official_domains=self.cfg.official_domains,
                signal=1.0,
                extra={"tag": tag, "repo": self.cfg.repo},
            ))
            break
        return out


class GitHubSearchSource:
    """Поиск новых репозиториев по темам (созданы после since). Обновления известных проектов
    ловим через их релизы (github_releases), иначе очередь забивают давно известные гиганты."""

    def __init__(self, cfg: SourceConfig, client: httpx.Client, since: datetime) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client
        self.since = since

    def _search(self, query: str) -> list[dict[str, Any]]:
        q = f"{query} created:>={self.since.date().isoformat()} stars:>={self.cfg.min_stars} archived:false"
        r = self.client.get(
            f"{API}/search/repositories",
            params={"q": q, "sort": "stars", "order": "desc", "per_page": self.cfg.per_query},
            headers=gh_headers(),
        )
        r.raise_for_status()
        return r.json().get("items", [])

    def collect(self) -> list[Candidate]:
        out: dict[str, Candidate] = {}
        for query in self.cfg.queries:
            for it in self._search(query):
                full = it["full_name"]
                if full in out:
                    continue
                out[full] = Candidate(
                    source=self.name,
                    source_type="github_search",
                    url=it["html_url"],
                    title=full,
                    summary=it.get("description") or "",
                    published_at=parse_dt(it.get("created_at")),
                    updated_at=parse_dt(it.get("pushed_at")),
                    signal=math.log10(1 + (it.get("stargazers_count") or 0)),
                    extra={
                        "stars": it.get("stargazers_count"),
                        "stars_per_day": stars_per_day(it.get("stargazers_count"), parse_dt(it.get("created_at"))),
                        "topics": it.get("topics") or [],
                        "license": (it.get("license") or {}).get("spdx_id"),
                        "query": query,
                        "repo_id": it.get("id"),  # переименованный репозиторий — тот же проект (решение 70)
                    },
                )
        return list(out.values())


def stars_per_day(stars: int | None, created: datetime | None) -> int | None:
    """Скорость набора звёзд с создания — сигнал популярности для оценки интереса (решение 70)."""
    if not stars or created is None:
        return None
    return round(stars / max((utc_now() - created).total_seconds() / 86400, 1))


_ROW = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
_ROW_REPO = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([\w.-]+/[\w.-]+)"', re.S)
_ROW_DESC = re.compile(r'<p class="col-9[^"]*">(.*?)</p>', re.S)
_ROW_ID = re.compile(r"repository_id&quot;:(\d+)")
_ROW_RECENT = re.compile(r"([\d,]+)\s+stars?\s+(?:today|this week|this month)")
_TAG = re.compile(r"<[^>]+>")


def parse_trending(page: str) -> list[dict[str, Any]]:
    """Строки страницы github.com/trending: репозиторий, описание, звёзды всего и за период."""
    out = []
    for row in _ROW.findall(page):
        if not (m := _ROW_REPO.search(row)):
            continue
        full = m.group(1)
        desc = _ROW_DESC.search(row)
        total = re.search(r'href="/' + re.escape(full) + r'/stargazers"[^>]*>.*?</svg>\s*([\d,]+)', row, re.S)
        recent = _ROW_RECENT.search(row)
        rid = _ROW_ID.search(row)
        out.append({
            "full_name": full,
            "description": html.unescape(_TAG.sub("", desc.group(1))).strip() if desc else "",
            "stars": int(total.group(1).replace(",", "")) if total else None,
            "stars_recent": int(recent.group(1).replace(",", "")) if recent else 0,
            "repo_id": int(rid.group(1)) if rid else None,
        })
    return out


class GitHubTrendingSource:
    """Проекты, которые набирают звёзды прямо сейчас (решение 70): страница github.com/trending за день и неделю.
    Поиск по темам находит только новые репозитории с нужными метками, а популярный проект часто без меток или
    старше месяца. Берём только про ИИ (include_text_regex по имени и описанию) — остальное модель не оценивает."""

    PERIODS = ("daily", "weekly")

    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def collect(self) -> list[Candidate]:
        include = [re.compile(p, re.IGNORECASE) for p in self.cfg.include_text_regex]
        rows: dict[str, dict[str, Any]] = {}
        for period in self.PERIODS:
            r = self.client.get("https://github.com/trending", params={"since": period}, follow_redirects=True)
            r.raise_for_status()
            for row in parse_trending(r.text):
                prev = rows.get(row["full_name"])
                if prev is None or row["stars_recent"] > prev["stars_recent"]:
                    rows[row["full_name"]] = row
        out = []
        for row in rows.values():
            text = f"{row['full_name']} {row['description']}"
            if include and not any(p.search(text) for p in include):
                continue
            if (row["stars_recent"] or 0) < self.cfg.min_stars:
                continue
            out.append(Candidate(
                source=self.name,
                source_type="github_trending",
                url=f"https://github.com/{row['full_name']}",
                title=row["full_name"],
                summary=row["description"],
                signal=math.log10(1 + row["stars_recent"]),
                extra={"stars": row["stars"], "stars_recent": row["stars_recent"], "repo_id": row["repo_id"]},
            ))
        return out
