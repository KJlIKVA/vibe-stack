from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .urls import canonical_url, dedup_keys, host_of, short_id

HARD_STOPS = (
    "ad",
    "no_primary_source",
    "unsafe_install",
    "outdated",
    "off_topic",
    "duplicate_suspected",
    "injection_detected",
)
HardStop = Literal[
    "ad", "no_primary_source", "unsafe_install", "outdated", "off_topic", "duplicate_suspected",
    "injection_detected",
]
Category = Literal[
    "tool", "skill_mcp", "trick", "case", "book_video", "benchmark", "analysis_candidate", "none"
]
UrgentEvent = Literal["new_model", "major_release", "pricing_or_limits_change", "vulnerability", "none"]

# категория промпта A → ключ рубрики в config.yaml
CATEGORY_TO_RUBRIC = {
    "tool": "tool",
    "skill_mcp": "skill_mcp",
    "trick": "trick",
    "case": "case",
    "book_video": "book_video",
    "benchmark": "benchmark",
    "analysis_candidate": "analysis",
}


class Status(StrEnum):
    DRAFT = "Черновик"
    PENDING = "На одобрении"
    APPROVED = "Одобрено"
    SENDING = "Отправляется"  # маркер перед отправкой: если дальше сбой, автоматически не переотправляем
    PUBLISHED = "Опубликовано"
    REJECTED = "Отклонено"
    ERROR = "Ошибка"


class Candidate(BaseModel):
    source: str
    source_type: str
    url: str
    title: str
    summary: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    whitelist: bool = False
    official_domains: list[str] = Field(default_factory=list)
    signal: float = 0.0  # звёзды/очки — только для порядка обработки, не для оценки
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return short_id(canonical_url(self.url))

    @property
    def canonical(self) -> str:
        return canonical_url(self.url)

    @property
    def domain(self) -> str:
        return host_of(self.url)

    @property
    def keys(self) -> list[str]:
        return dedup_keys(self.url, self.title)

    @property
    def freshest(self) -> datetime | None:
        dates = [d for d in (self.published_at, self.updated_at) if d]
        return max(dates) if dates else None

    def for_prompt(self) -> dict[str, Any]:
        """То, что видит модель в <candidate>: без внутренних полей."""
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "summary": self.summary[:1500],
            "source": self.source,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "extra": {k: v for k, v in self.extra.items()
                      if k in ("stars", "points", "topics", "tag", "license", "media", "minutes")},
        }


class FetchedDoc(BaseModel):
    url: str
    final_url: str
    ok: bool
    http_status: int = 0
    fetched_at: datetime
    title: str = ""
    published_meta: str | None = None
    updated_meta: str | None = None  # для репозиториев — последний push или релиз
    text: str = ""
    error: str | None = None
    image: str | None = None  # главная картинка страницы (og:image) или карточка репозитория GitHub
    figures: list[str] = Field(default_factory=list)  # картинки статьи по порядку (без логотипов и иконок)


class ImagePick(BaseModel):
    """Выбор картинки для поста о новой модели (шаг image): номер картинки статьи или -1."""

    index: int
    kind: Literal["benchmark", "pricing", "none"]


class Scores(BaseModel):
    novelty: int = Field(ge=0, le=3)
    usefulness: int = Field(ge=0, le=5)
    verifiability: int = Field(ge=0, le=3)
    substance: int = Field(ge=0, le=2)
    audience_fit: int = Field(ge=0, le=2)

    @property
    def total(self) -> int:
        return self.novelty + self.usefulness + self.verifiability + self.substance + self.audience_fit


class ScoreResult(BaseModel):
    id: str
    category: Category
    scores: Scores
    hard_stops: list[HardStop] = Field(default_factory=list)
    reason: str = ""
    claims: list[str] = Field(default_factory=list)

    @field_validator("reason")
    @classmethod
    def _cut_reason(cls, v: str) -> str:
        return v[:200]

    @field_validator("claims")
    @classmethod
    def _cut_claims(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c.strip()][:5]


class Check(BaseModel):
    claim: str
    status: Literal["supported", "not_supported", "unclear"]
    evidence: str = ""


class VerifyResult(BaseModel):
    verdict: Literal["pass", "fail"]
    fail_reason: str = ""
    checks: list[Check] = Field(default_factory=list)
    approved_claims: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    id: str
    event: UrgentEvent
    hard_stops: list[HardStop] = Field(default_factory=list)
    reason: str = ""
    claims: list[str] = Field(default_factory=list)

    @field_validator("reason")
    @classmethod
    def _cut_reason(cls, v: str) -> str:
        return v[:200]

    @field_validator("claims")
    @classmethod
    def _cut_claims(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c.strip()][:5]


class PostRecord(BaseModel):
    """Строка таблицы Posts — общий формат для Notion и локальной доски."""

    ref: str | None = None  # id страницы Notion или локальный id
    title: str
    rubric: str
    status: Status
    mode: Literal["auto", "approve"] = "auto"
    urgent: bool = False
    score: int | None = None
    scores: dict[str, int] | None = None
    hard_stops: list[str] = Field(default_factory=list)
    reject_reason: str = ""
    source_url: str = ""
    source_domain: str = ""
    found_at: datetime | None = None
    published_at: datetime | None = None
    tg_message_id: int | None = None
    html: str = ""
    verify: dict[str, Any] | None = None
    candidate_id: str | None = None
    planned_at: datetime | None = None  # время публикации из плана дня (можно поменять в Notion)


class GlossaryResult(BaseModel):
    id: str
    term: str
    has_definition: bool
    hard_stops: list[HardStop] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    example: str = ""
    not_to_confuse: str = ""

    @field_validator("claims")
    @classmethod
    def _cut_claims(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c.strip()][:4]
