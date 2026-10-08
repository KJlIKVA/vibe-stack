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


# общий префикс рубрики — не тема: иначе «Слово дня: LLM» и «Слово дня: RAG» совпадают по Жаккару
_RUBRIC_PREFIX = re.compile(r"^\s*слово дня:\s*")


def topic_tokens(title: str, url: str = "") -> set[str]:
    if m := _RUBRIC_PREFIX.match(title.lower()):
        return {"term:" + title.lower()[m.end():].strip()}  # тема «Слова дня» — сам термин
    words = re.findall(r"[a-zа-яё0-9][a-zа-яё0-9+.#-]{2,}", title.lower())
    tokens = {w.strip(".-") for w in words if w not in _STOP}
    if repo := github_repo(url):
        tokens.add(f"repo:{repo}")
    return {t for t in tokens if t}


def domain_key(url: str, domain: str = "") -> str:
    """«Домен» для правила «не больше N постов с домена в день». У репозиториев GitHub — владелец:
    разные проекты на github.com — разные источники, а не один сайт."""
    if repo := github_repo(url):
        return f"github.com/{repo.split('/')[0]}"
    return domain or host_of(url)


# группа «Книги/видео» по хештегу поста (решение 52): видео и подкасты — «что посмотреть и послушать» с дневным
# лимитом, книги — с недельным. Лимиты планировщика задаются и на рубрику, и на «рубрика:группа».
MEDIA_GROUPS = {"#видео": "watch", "#подкаст": "watch", "#книга": "book"}
_LAST_HASHTAG = re.compile(r"#[\w]+(?=\s*$)")


def media_group(post_html: str) -> str | None:
    m = _LAST_HASHTAG.search(post_html or "")
    return MEDIA_GROUPS.get(m.group(0)) if m else None


def limit_keys(rubric: str, group: str | None) -> list[str]:
    return [rubric, f"{rubric}:{group}"] if group else [rubric]


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
    soft_rubric_repeat: bool = False,
    groups: dict[str, str | None] | None = None,
) -> Pick:
    """history — опубликованное (обычное и срочное) минимум за topic_repeat_days дней.
    groups — ref поста (в очереди и в истории) → группа «Книги/видео» (media_group), для лимитов по группам."""
    weekly_min = cfg.weekly_min if weekly_min is None else weekly_min
    weekly_max = cfg.weekly_max if weekly_max is None else weekly_max
    regular = [h for h in history if h.counts_regular and not h.urgent]
    last_rubric = max(regular, key=lambda h: h.published_at).rubric if regular else None
    domains_today = [domain_key(h.source_url, h.domain) for h in history if h.local_date == today]
    groups = groups or {}

    def keys(rubric: str, ref: str | None) -> list[str]:
        return limit_keys(rubric, groups.get(ref or ""))

    wk = week_start(today)
    rubric_today: dict[str, int] = {}  # ключи — рубрика и «рубрика:группа»
    week_counts: dict[str, int] = {}
    for h in history:
        for k in keys(h.rubric, h.ref):
            if h.local_date == today:
                rubric_today[k] = rubric_today.get(k, 0) + 1
            if h.local_date >= wk:
                week_counts[k] = week_counts.get(k, 0) + 1
    queued: dict[str, int] = {}
    for p in queue:
        for k in keys(p.rubric, p.ref):
            queued[k] = queued.get(k, 0) + 1

    def limit(key: str, normal: dict[str, int], backlog: dict[str, int], done: dict[str, int]) -> int | None:
        """Максимум за день или неделю; при большой очереди (решения 50, 52) — выше. Очередь считаем вместе
        с вышедшим за период: иначе каждый выпущенный пост уменьшал бы её, и очередь ровно из backlog_queue
        давала бы обычный максимум."""
        if (bmx := backlog.get(key)) is not None and queued.get(key, 0) + done.get(key, 0) >= cfg.backlog_queue:
            return bmx
        return normal.get(key)
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
        if last_rubric and p.rubric == last_rubric and not soft_rubric_repeat:
            reasons.append("та же рубрика подряд")
        domain = domain_key(p.source_url, p.source_domain)
        if domains_today.count(domain) >= cfg.max_per_domain_per_day:
            reasons.append(f"домен {domain} уже был сегодня")
        for k in keys(p.rubric, p.ref):
            if (mx := limit(k, weekly_max, cfg.backlog_weekly_max, week_counts)) is not None \
                    and week_counts.get(k, 0) >= mx:
                reasons.append("недельный максимум рубрики")
            if (dmx := limit(k, cfg.daily_max, cfg.backlog_daily_max, rubric_today)) is not None \
                    and rubric_today.get(k, 0) >= dmx:
                reasons.append("дневной максимум рубрики")
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

    def priority(p: PostRecord) -> tuple[int, int, int, float]:
        need = weekly_min.get(p.rubric, 0) - week_counts.get(p.rubric, 0)
        found = p.found_at.timestamp() if p.found_at else 0.0
        repeat = 1 if last_rubric and p.rubric == last_rubric else 0
        # 1) рубрики с невыполненной недельной квотой, 2) другая рубрика, чем у предыдущего поста,
        # 3) баллы, 4) кто дольше ждёт
        return (0 if need > 0 else 1, repeat, -(p.score or 0), found)

    return Pick(min(ok, key=priority), skipped)
