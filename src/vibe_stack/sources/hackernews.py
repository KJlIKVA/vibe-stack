"""Hacker News через официальный поиск Algolia (hn.algolia.com/api). HN — только способ найти:
в пост идёт ссылка на первоисточник (url истории), а не на обсуждение."""

from __future__ import annotations

import math
from datetime import datetime

import httpx

from ..config import SourceConfig
from ..models import Candidate
from ..timeutil import parse_dt

API = "https://hn.algolia.com/api/v1/search_by_date"


class HackerNewsSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client, since: datetime) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client
        self.since = since

    def collect(self) -> list[Candidate]:
        out: dict[str, Candidate] = {}
        ts = int(self.since.timestamp())
        for query in self.cfg.queries:
            r = self.client.get(API, params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{ts},points>={self.cfg.min_points}",
                "hitsPerPage": self.cfg.per_query,
            })
            r.raise_for_status()
            for hit in r.json().get("hits", []):
                url = hit.get("url")
                if not url or url in out:  # Ask HN без ссылки — не первоисточник
                    continue
                out[url] = Candidate(
                    source=self.name,
                    source_type="hackernews",
                    url=url,
                    title=(hit.get("title") or "").strip(),
                    published_at=parse_dt(hit.get("created_at")),
                    signal=math.log10(1 + (hit.get("points") or 0)),
                    extra={"points": hit.get("points"), "hn_id": hit.get("objectID"), "query": query},
                )
        return list(out.values())
