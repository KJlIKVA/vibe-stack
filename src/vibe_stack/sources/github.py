from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import httpx

from ..config import SourceConfig, env
from ..models import Candidate
from ..timeutil import parse_dt

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
                        "topics": it.get("topics") or [],
                        "license": (it.get("license") or {}).get("spdx_id"),
                        "query": query,
                    },
                )
        return list(out.values())
