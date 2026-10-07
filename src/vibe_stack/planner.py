"""Планировщик рубрик (раздел 4): выбирает из очереди по правилам, а не «лучшее по баллам»."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Planner
from .models import PostRecord
from .storage import PublishedRow
from .timeutil import week_start
from .urls import canonical_url, github_repo, host_of

_STOP = {
    "the", "and", "for", "with", "your", "you", "from", "that", "this", "into", "how", "new", "now", "are",
    "что", "как", "это", "для", "или", "при", "все", "его", "она", "они", "так", "уже", "вас", "ваш",
    "v", "vs",
}


def topic_tokens(title: str, url: str = "") -> set[str]:
    words = re.findall(r"[a-zа-яё0-9][a-zа-яё0-9+.#-]{2,}", title.lower())
    tokens = {w.strip(".-") for w in words if w not in _STOP}
    if repo := github_repo(url):
        tokens.add(f"repo:{repo}")
    return {t for t in tokens if t}


def same_topic(a: set[str], b: set[str], threshold: float) -> bool:
    if not a or not b:
        return False
    repos_a = {t for t in a if t.startswith("repo:")}
    if repos_a and repos_a & b:
        return True
    return len(a & b) / len(a | b) >= threshold


@dataclass
class Pick:
    post: PostRecord | None
    skipped: dict[str, list[str]] = field(default_factory=dict)  # ref → причины

    @property
    def reason(self) -> str:
        if self.post:
            return "ok"
        if not self.skipped:
            return "очередь пуста"
        return "нет подходящих: " + "; ".join(f"{k}: {','.join(v)}" for k, v in list(self.skipped.items())[:5])


def pick_next(
    queue: list[PostRecord],
    history: list[PublishedRow],
    *,
    today: date,
    now: datetime,
    cfg: Planner,
    rubric_enabled: dict[str, bool],
    weekly_min: dict[str, int] | None = None,
    weekly_max: dict[str, int] | None = None,
) -> Pick:
    """history — опубликованное (обычное и срочное) минимум за topic_repeat_days дней."""
    weekly_min = cfg.weekly_min if weekly_min is None else weekly_min
    weekly_max = cfg.weekly_max if weekly_max is None else weekly_max
    regular = [h for h in history if h.counts_regular and not h.urgent]
    last_rubric = max(regular, key=lambda h: h.published_at).rubric if regular else None
    domains_today = [h.domain for h in history if h.local_date == today]
    wk = week_start(today)
    week_counts: dict[str, int] = {}
    for h in history:
        if h.local_date >= wk:
            week_counts[h.rubric] = week_counts.get(h.rubric, 0) + 1
    recent_cutoff = now - timedelta(days=cfg.topic_repeat_days)
    recent_topics = [
        (topic_tokens(h.title, h.source_url), canonical_url(h.source_url) if h.source_url else "")
        for h in history if h.published_at >= recent_cutoff
    ]

    ok: list[PostRecord] = []
    skipped: dict[str, list[str]] = {}
    for p in queue:
        reasons = []
        if not rubric_enabled.get(p.rubric, False):
            reasons.append("рубрика выключена")
        if last_rubric and p.rubric == last_rubric:
            reasons.append("та же рубрика подряд")
        domain = p.source_domain or host_of(p.source_url)
        if domains_today.count(domain) >= cfg.max_per_domain_per_day:
            reasons.append(f"домен {domain} уже был сегодня")
        if (mx := weekly_max.get(p.rubric)) is not None and week_counts.get(p.rubric, 0) >= mx:
            reasons.append("недельный максимум рубрики")
        toks = topic_tokens(p.title, p.source_url)
        canon = canonical_url(p.source_url) if p.source_url else ""
        if any((canon and canon == c) or same_topic(toks, t, cfg.topic_similarity) for t, c in recent_topics):
            reasons.append(f"тема была за {cfg.topic_repeat_days} дн.")
        if reasons:
            skipped[p.ref or p.title] = reasons
        else:
            ok.append(p)
    if not ok:
        return Pick(None, skipped)

    def priority(p: PostRecord) -> tuple[int, int, float]:
        need = weekly_min.get(p.rubric, 0) - week_counts.get(p.rubric, 0)
        found = p.found_at.timestamp() if p.found_at else 0.0
        # 1) рубрики с невыполненной недельной квотой, 2) баллы, 3) кто дольше ждёт
        return (0 if need > 0 else 1, -(p.score or 0), found)

    return Pick(min(ok, key=priority), skipped)
