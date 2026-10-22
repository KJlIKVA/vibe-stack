"""Планировщик рубрик (раздел 4): выбирает из очереди по правилам, а не «лучшее по баллам»."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Planner, SourceConfig
from .models import PostRecord
from .storage import PublishedRow
from .timeutil import week_start
from .urls import canonical_url, github_repo, host_of, same_site

_STOP = {
    "the", "and", "for", "with", "your", "you", "from", "that", "this", "into", "how", "new", "now", "are",
    "что", "как", "это", "для", "или", "при", "все", "его", "она", "они", "так", "уже", "вас", "ваш",
    "v", "vs",
}


# имя проекта, которое не говорит, какой это проект: «MCP: …» и репозиторий x/mcp — не одна новость
_GENERIC_NAMES = {"mcp", "api", "cli", "sdk", "app", "docs", "agent", "agents", "skill", "skills", "tool", "tools",
                  "awesome", "github", "openai", "google", "anthropic", "microsoft", "claude", "gemini", "codex"}
_TITLE_PREFIX = re.compile(r"^(?:github\s+-\s+|(?:show|ask|launch)\s+hn:\s*)", re.I)
_LEAD_END = re.compile(r"\s*[:–—|]\s*|\s+-\s+")


def _name(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]", "", s.lower())


def topic_tokens(title: str, url: str = "") -> set[str]:
    # короткие числа тоже слова темы: «Mistral Large 4» и «Mistral Large 3» — разные модели
    words = re.findall(r"[a-zа-яё0-9][a-zа-яё0-9+.#-]{2,}|\d+(?:\.\d+)*", title.lower())
    tokens = {w.strip(".-") for w in words if w not in _STOP}
    # проект (решение 62): у репозитория — его имя, у остальных — начало заголовка до «:», «—», « - ».
    # Репозиторий google-ai-edge/ml-drift и пост «ML Drift: Next-Gen GPU…» — одна новость
    if repo := github_repo(url):
        tokens.add(f"repo:{repo}")
        name = _name(repo.split("/")[1])
        prefix = "proj"
    else:
        name = _name(_LEAD_END.split(_TITLE_PREFIX.sub("", title.strip()), maxsplit=1)[0].rsplit("/", 1)[-1])
        prefix = "lead"
    if len(name) >= 3 and name not in _GENERIC_NAMES:
        tokens.add(f"{prefix}:{name}")
    return {t for t in tokens if t}


def _without(tokens: set[str], *prefixes: str) -> set[str]:
    return {t for t in tokens if not t.startswith(prefixes)}


def same_project(a: set[str], b: set[str]) -> bool:
    """Пост о репозитории и пост, чей заголовок начинается с имени этого проекта (ML Drift, MXC). Два репозитория
    сравнивает правило repo:, два обычных заголовка с общим началом («OpenAI API: …») — не одна новость."""
    def names(t: set[str], prefix: str) -> set[str]:
        return {x.split(":", 1)[1] for x in t if x.startswith(prefix)}

    # репозиторий с тем же именем у другого владельца — переименованный или перенесённый проект: 18 и 19.10
    # anujasid/blender-ai-mcp-addon и LiCinCan/blender-ai-mcp-addon вышли двумя постами (решение 70)
    if names(a, "proj:") & names(b, "proj:") and not names(a, "repo:") & names(b, "repo:"):
        return True
    return bool(names(a, "proj:") & names(b, "lead:") or names(b, "proj:") & names(a, "lead:"))


def domain_key(url: str, domain: str = "") -> str:
    """«Домен» для правила «не больше N постов с домена в день». У репозиториев GitHub — владелец:
    разные проекты на github.com — разные источники, а не один сайт."""
    if repo := github_repo(url):
        return f"github.com/{repo.split('/')[0]}"
    return domain or host_of(url)


@dataclass(frozen=True)
class Official:
    """Официальные источники (решение 63): сайты компаний и репозитории их инструментов из белого списка
    (openai.com, anthropic.com, deepmind.google, anthropics/claude-code, openai/codex, google-gemini/gemini-cli…).
    github.com целиком официальным не считается — только репозитории источников github_releases."""

    hosts: frozenset[str] = frozenset()
    repos: frozenset[str] = frozenset()

    @classmethod
    def from_sources(cls, sources: Iterable[SourceConfig]) -> Official:
        hosts: set[str] = set()
        repos: set[str] = set()
        for s in sources:
            if not s.whitelist:
                continue
            if s.repo:
                repos.add(s.repo.lower())
            else:
                hosts.update(d.lower() for d in s.official_domains if d.lower() != "github.com")
        return cls(frozenset(hosts), frozenset(repos))

    def __call__(self, url: str) -> bool:
        if repo := github_repo(url):
            return repo in self.repos
        host = host_of(url)
        return bool(host) and any(same_site(host, d) for d in self.hosts)


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
    if (repos_a and repos_a & b) or same_project(a, b):
        return True
    a, b = _without(a, "proj:", "lead:"), _without(b, "proj:", "lead:")
    return bool(a | b) and len(a & b) / len(a | b) >= threshold


def same_news(a: set[str], b: set[str]) -> bool:
    """Строже same_topic — для отсева до оценки (решение 59): ошибка здесь стоит потерянной новости.

    Общий репозиторий не в счёт (claude-code v2.1.293 и v2.1.294 — разные выпуски), разные числа — разные
    новости (раунды инвестиций, версии). Одна новость — почти одинаковые заголовки или короткий заголовок,
    целиком входящий в длинный, если совпадает номер версии или хотя бы три слова. Репозиторий и пост о том же
    проекте (same_project) — одна новость.
    """
    if same_project(a, b):
        return True
    a, b = _without(a, "repo:", "proj:", "lead:"), _without(b, "repo:", "proj:", "lead:")
    common = a & b
    if len(common) < 2:
        return False
    nums_a = {t for t in a if any(ch.isdigit() for ch in t)}
    nums_b = {t for t in b if any(ch.isdigit() for ch in t)}
    if nums_a and nums_b and not (nums_a <= nums_b or nums_b <= nums_a):
        return False
    if len(common) / len(a | b) >= 0.8:
        return True
    return len(common) / min(len(a), len(b)) >= 0.8 and (bool(common & nums_a) or len(common) >= 3)


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
    official: Callable[[str], bool] | None = None,
) -> Pick:
    """history — опубликованное (обычное и срочное) минимум за topic_repeat_days дней.
    groups — ref поста (в очереди и в истории) → группа «Книги/видео» (media_group), для лимитов по группам.
    official — официальный ли источник по ссылке (Official): у таких постов приоритет, нет лимита на домен, и разные
    публикации одного официального источника не считаются повтором темы (решение 63)."""
    is_official = official or (lambda _url: False)
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
        (topic_tokens(h.title, h.source_url), canonical_url(h.source_url) if h.source_url else "",
         domain_key(h.source_url, h.domain) if h.source_url and is_official(h.source_url) else None)
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
        own = domain if p.source_url and is_official(p.source_url) else None  # официальный источник поста
        # лимит на домен — не для официальных и не для видео, подкастов и книг: у них свои дневные лимиты,
        # а youtube.com — площадка, а не источник (решение 63)
        if not own and not groups.get(p.ref or "") and domains_today.count(domain) >= cfg.max_per_domain_per_day:
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
        # другая страница того же официального источника — отдельный выпуск: claude-code v2.1.296 после v2.1.295,
        # второй анонс Gemini в блоге DeepMind (решение 63)
        if any((canon and canon == c) or (not (own and own == src) and same_topic(toks, t, cfg.topic_similarity))
               for t, c, src in recent_topics):
            reasons.append(f"тема была за {cfg.topic_repeat_days} дн.")
        if reasons:
            skipped[p.ref or p.title] = reasons
        else:
            ok.append(p)
    if not ok:
        return Pick(None, skipped)

    def priority(p: PostRecord) -> tuple[int, int, int, int, int, float]:
        need = weekly_min.get(p.rubric, 0) - week_counts.get(p.rubric, 0)
        found = p.found_at.timestamp() if p.found_at else 0.0
        repeat = 1 if last_rubric and p.rubric == last_rubric else 0
        official_first = 0 if p.source_url and is_official(p.source_url) else 1
        interest = (p.scores or {}).get("interest", 0)
        # 1) рубрики с невыполненной недельной квотой, 2) официальные выпуски (решение 63), 3) другая рубрика, чем
        # у предыдущего поста, 4) интерес (решение 70), 5) баллы, 6) свежее — раньше (решение 59)
        return (0 if need > 0 else 1, official_first, repeat, -interest, -(p.score or 0), -found)

    return Pick(min(ok, key=priority), skipped)
