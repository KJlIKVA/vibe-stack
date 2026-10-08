"""Загрузка config.yaml и секретов из окружения.

Модели, лимиты, пороги и расписание живут в config.yaml; секреты — только в переменных окружения.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "ADMIN_CHAT_ID",
    "NOTION_TOKEN",
    "NOTION_ROOT_PAGE_ID",
    "GITHUB_TOKEN",
    "TELEGRAPH_TOKEN",
    "AA_API_KEY",
)


class StepConfig(BaseModel):
    model: str
    effort: str = "medium"
    max_output_tokens: int = 8000


class Price(BaseModel):
    input: float
    output: float
    cached_input: float | None = None


class LLMConfig(BaseModel):
    provider: Literal["openai"] = "openai"
    base_url: str | None = None
    max_calls_per_run: int = 60
    daily_budget_usd: float | None = 3.0  # None — без лимита в долларах
    # модель → токенов за сутки UTC (вход + выход, включая рассуждение); вызов, который может выйти за лимит,
    # не делается. Сутки по UTC — как сбрасываются дневные лимиты OpenAI.
    daily_token_limits: dict[str, int] = Field(default_factory=dict)
    request_timeout_s: float = 180
    max_attempts: int = 2  # попытки на один вызов; каждая учитывается в лимите и бюджете
    prices_per_1m: dict[str, Price] = Field(default_factory=dict)
    steps: dict[str, StepConfig]


class ChannelConfig(BaseModel):
    name: str = "Vibe Stack"
    tz: str = "Europe/Moscow"
    username: str = ""


class Limits(BaseModel):
    regular_per_day: int = 3
    urgent_per_day: int = 2


class Gate(BaseModel):
    min_total: int = 10
    min_verifiability: int = 2
    max_age_days: int = 30
    min_approved_claims: int = 2


class Schedule(BaseModel):
    publish_slots: list[str] = Field(default_factory=lambda: ["10:00", "14:00", "18:00"])
    slot_window_minutes: int = 120
    queue_max_age_days: int = 5

    @field_validator("publish_slots")
    @classmethod
    def _check_slots(cls, v: list[str]) -> list[str]:
        for s in v:
            hh, mm = s.split(":")
            if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError(f"bad slot {s}")
        return sorted(v)


class Planner(BaseModel):
    topic_repeat_days: int = 7
    topic_similarity: float = 0.5
    max_per_domain_per_day: int = 1
    weekly_min: dict[str, int] = Field(default_factory=dict)
    weekly_max: dict[str, int] = Field(default_factory=dict)
    daily_max: dict[str, int] = Field(default_factory=dict)  # не больше стольких постов рубрики в день


class Collect(BaseModel):
    max_candidates_to_score: int = 25
    max_per_source: int = 8
    max_minutes: float = 20  # дедлайн сбора: job в Actions не должен убиваться посреди записи
    code_injection_guard: bool = True


class Urgent(BaseModel):
    max_item_age_hours: int = 24
    events: list[str] = Field(default_factory=list)
    new_model_max_chars: int = 900  # пост о новой модели: цена и сравнение не помещаются в 500 знаков


class ImagesConfig(BaseModel):
    """Картинка поста — большое превью над текстом (решение 47)."""

    enabled: bool = True
    max_figures: int = 8  # сколько картинок статьи показывать модели, чтобы выбрать таблицу бенчмарков/цен


class Fetch(BaseModel):
    timeout_s: float = 20
    max_bytes: int = 3_000_000
    max_doc_chars: int = 24_000
    user_agent: str = "VibeStackBot/0.1"
    # сайты, которые не пускают роботов из облака: при 403 — последняя копия из web.archive.org (решение 48)
    archive_fallback_hosts: list[str] = Field(default_factory=list)


class Dedup(BaseModel):
    seen_ttl_days: int = 60


class Prefilter(BaseModel):
    blocked_domains: list[str] = Field(default_factory=list)
    pirate_domains: list[str] = Field(default_factory=list)
    affiliate_params: list[str] = Field(default_factory=list)


class GlossaryTerm(BaseModel):
    term: str
    aliases: list[str] = Field(default_factory=list)
    source: str


class GlossaryConfig(BaseModel):
    telegraph_page: bool = True
    telegraph_path: str = ""  # путь страницы на telegra.ph (публичный); токен — секрет TELEGRAPH_TOKEN
    telegraph_url: str = ""
    terms: list[GlossaryTerm] = Field(default_factory=list)


class LeaderboardConfig(BaseModel):
    key: str
    type: Literal["arena_hf", "artificial_analysis"]
    label: str
    dataset_config: str | None = None
    enabled: bool = True


class Rubric(BaseModel):
    emoji: str
    hashtag: str
    title: str
    mode: Literal["auto", "approve"] = "auto"
    enabled: bool = True
    max_chars: int = 900
    overlay: str = "standard"


class SourceConfig(BaseModel):
    name: str
    type: Literal["rss", "github_releases", "github_search", "hackernews", "sitemap", "md_changelog", "youtube",
                  "fixture"]
    url: str | None = None
    repo: str | None = None
    path_prefix: str | None = None
    whitelist: bool = False
    official_domains: list[str] = Field(default_factory=list)
    contours: list[str] = Field(default_factory=lambda: ["collect"])
    enabled: bool = True
    queries: list[str] = Field(default_factory=list)
    per_query: int = 10
    min_stars: int = 0
    min_points: int = 0
    skip_title_regex: list[str] = Field(default_factory=list)
    include_title_regex: list[str] = Field(default_factory=list)  # непусто — берём только такие заголовки (книги по ИИ)
    channels: list[str] = Field(default_factory=list)  # youtube: id каналов UC…
    title_prefix: str = ""  # md_changelog: «OpenAI API: » перед текстом записи
    max_age_days: int | None = None  # свой предел свежести вместо gate.max_age_days (changelog: только последние дни)
    # агрегатор ссылок (Reddit): берём внешнюю ссылку «[link]» из записи; записи без неё (обсуждения, картинки,
    # видео на этих хостах) пропускаем — их страницы роботам закрыты, а первоисточник — по внешней ссылке
    aggregator_hosts: list[str] = Field(default_factory=list)


class SandboxConfig(BaseModel):
    """Песочница (фаза 3): установка пакета находки и запуск --help в изолированном контейнере GitHub Actions."""

    enabled: bool = True
    rubrics: list[str] = Field(default_factory=lambda: ["tool", "skill_mcp"])
    ecosystems: list[str] = Field(default_factory=lambda: ["pypi", "npm"])
    max_wait_minutes: int = 120  # столько публикация ждёт результата, потом пост выходит без пометки
    max_requests_per_run: int = 10
    max_age_hours: int = 24      # заявку старше этого в песочницу не отдаём
    max_attempts: int = 2        # столько раз запуск может оборваться на заявке, потом она «не запустилась»
    # True — пакет только с provenance из репозитория поста; False — ещё и по манифесту репозитория (решение 44)
    require_provenance: bool = False


class DigestConfig(BaseModel):
    enabled: bool = True  # сводка дня админу (время — cron в .github/workflows/digest.yml)


class Config(BaseModel):
    channel: ChannelConfig = Field(default_factory=ChannelConfig)
    llm: LLMConfig
    limits: Limits = Field(default_factory=Limits)
    gate: Gate = Field(default_factory=Gate)
    schedule: Schedule = Field(default_factory=Schedule)
    planner: Planner = Field(default_factory=Planner)
    collect: Collect = Field(default_factory=Collect)
    urgent: Urgent = Field(default_factory=Urgent)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    fetch: Fetch = Field(default_factory=Fetch)
    dedup: Dedup = Field(default_factory=Dedup)
    prefilter: Prefilter = Field(default_factory=Prefilter)
    rubrics: dict[str, Rubric]
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
    leaderboards: list[LeaderboardConfig] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)

    def rubric(self, key: str) -> Rubric:
        return self.rubrics[key]


def load_config(path: str | Path = "config.yaml") -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Config.model_validate(data)


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный загрузчик .env для локального запуска: не перетирает уже заданные переменные."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, required: bool = False) -> str | None:
    value = os.environ.get(name) or None
    if required and not value:
        raise MissingSecret(name)
    return value


def secret_values() -> list[str]:
    """Значения секретов — чтобы вычищать их из логов."""
    return [v for n in SECRET_ENV_NAMES if (v := os.environ.get(n)) and len(v) >= 6]


class MissingSecret(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Не задана переменная окружения {name}")
        self.name = name
