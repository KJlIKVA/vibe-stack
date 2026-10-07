"""Источники кандидатов. Каждый адаптер возвращает список Candidate; сбой одного не ломает остальные."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta
from typing import Protocol

import httpx

from ..config import SourceConfig
from ..models import Candidate
from ..timeutil import Clock
from . import github, hackernews, rss, sitemap

log = logging.getLogger(__name__)


class Source(Protocol):
    name: str

    def collect(self) -> list[Candidate]: ...


def build_source(cfg: SourceConfig, client: httpx.Client, clock: Clock, max_age_days: int) -> Source:
    since = clock() - timedelta(days=max_age_days)
    match cfg.type:
        case "rss":
            return rss.RSSSource(cfg, client)
        case "github_releases":
            return github.GitHubReleasesSource(cfg, client)
        case "github_search":
            return github.GitHubSearchSource(cfg, client, since)
        case "hackernews":
            return hackernews.HackerNewsSource(cfg, client, since)
        case "sitemap":
            return sitemap.SitemapSource(cfg, client, since)
    raise ValueError(f"неизвестный тип источника {cfg.type}")


def collect_all(sources: Iterable[Source]) -> tuple[list[Candidate], dict[str, str]]:
    """Все кандидаты и ошибки по источникам (источник недоступен → лог, остальные работают)."""
    out: list[Candidate] = []
    errors: dict[str, str] = {}
    for src in sources:
        try:
            got = src.collect()
            log.info("источник %s: %d кандидатов", src.name, len(got))
            out.extend(got)
        except Exception as e:
            log.warning("источник %s недоступен: %s", src.name, e)
            errors[src.name] = f"{type(e).__name__}: {str(e)[:200]}"
    return out, errors
