"""Доска — то, что видит и правит человек: посты со статусами, рубрики, источники, настройки.

Реализации: NotionBoard (боевая) и LocalBoard (JSON-файл для dry-run и тестов). Код читает с доски
только статусы постов, флажок «Пауза», режимы рубрик и флаги источников.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .models import PostRecord, Status


class BoardSettings(BaseModel):
    pause: bool = False
    regular_per_day: int | None = None
    urgent_per_day: int | None = None
    publish_slots: list[str] | None = None
    tz: str | None = None


class RubricOverride(BaseModel):
    mode: str | None = None  # auto | approve
    enabled: bool | None = None
    weekly_quota: int | None = None


class SourceOverride(BaseModel):
    whitelist: bool | None = None
    enabled: bool | None = None


class BoardUnavailable(RuntimeError):
    """Доску не удалось прочитать или записать. Публикующие контуры в этом случае ничего не публикуют."""


def validate_settings(s: BoardSettings) -> BoardSettings:
    """Настройки с доски проверяются так же строго, как config.yaml; ошибка — BoardUnavailable (fail-closed)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from .config import Schedule

    if s.publish_slots is not None:
        try:
            s = s.model_copy(update={"publish_slots": Schedule(publish_slots=s.publish_slots).publish_slots})
        except (ValueError, TypeError) as e:
            raise BoardUnavailable(f"неверное «Время публикаций»: {s.publish_slots} (нужно ЧЧ:ММ через запятую)") from e
    if s.tz:
        try:
            ZoneInfo(s.tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise BoardUnavailable(f"неверный часовой пояс TZ: {s.tz!r}") from e
    for name in ("regular_per_day", "urgent_per_day"):
        v = getattr(s, name)
        if v is not None and v < 0:
            raise BoardUnavailable(f"отрицательный лимит {name}")
    return s


class Board(Protocol):
    def settings(self) -> BoardSettings: ...
    def rubric_overrides(self) -> dict[str, RubricOverride]: ...
    def source_overrides(self) -> dict[str, SourceOverride]: ...
    def add_post(self, post: PostRecord) -> str: ...
    def update_post(self, ref: str, **fields: Any) -> None: ...
    def posts_with_status(self, status: Status) -> list[PostRecord]: ...
    def published_since(self, since: datetime) -> list[PostRecord]: ...
    def add_glossary(self, entry: GlossaryEntry) -> None: ...


class GlossaryEntry(BaseModel):
    term: str
    definition: str
    source_url: str
    published_at: datetime
    post_url: str | None = None


class _LocalData(BaseModel):
    settings: BoardSettings = Field(default_factory=BoardSettings)
    rubrics: dict[str, RubricOverride] = Field(default_factory=dict)
    sources: dict[str, SourceOverride] = Field(default_factory=dict)
    posts: list[PostRecord] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)


class LocalBoard:
    """Доска в JSON-файле. Для dry-run без Notion и для тестов."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.exists():
            self.data = _LocalData.model_validate_json(self.path.read_text(encoding="utf-8"))
        else:
            self.data = _LocalData()
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.data.model_dump_json(indent=2), encoding="utf-8")

    def settings(self) -> BoardSettings:
        return validate_settings(self.data.settings)

    def set_settings(self, **fields: Any) -> None:
        self.data.settings = self.data.settings.model_copy(update=fields)
        self._save()

    def rubric_overrides(self) -> dict[str, RubricOverride]:
        return self.data.rubrics

    def source_overrides(self) -> dict[str, SourceOverride]:
        return self.data.sources

    def add_post(self, post: PostRecord) -> str:
        ref = post.ref or f"local-{uuid.uuid4().hex[:10]}"
        self.data.posts.append(post.model_copy(update={"ref": ref}))
        self._save()
        return ref

    def update_post(self, ref: str, **fields: Any) -> None:
        for i, p in enumerate(self.data.posts):
            if p.ref == ref:
                self.data.posts[i] = p.model_copy(update=fields)
                self._save()
                return
        raise KeyError(ref)

    def posts_with_status(self, status: Status) -> list[PostRecord]:
        return [p for p in self.data.posts if p.status == status]

    def published_since(self, since: datetime) -> list[PostRecord]:
        return [p for p in self.data.posts
                if p.status == Status.PUBLISHED and p.published_at and p.published_at >= since]

    def add_glossary(self, entry: GlossaryEntry) -> None:
        self.data.glossary.append(entry)
        self._save()

    def get(self, ref: str) -> PostRecord:
        return next(p for p in self.data.posts if p.ref == ref)


class RecordingBoard:
    """Обёртка для dry-run поверх боевой доски: читает по-настоящему, записи только складывает в файл."""

    def __init__(self, inner: Board, log_path: str | Path) -> None:
        self.inner = inner
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, op: str, payload: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"op": op, **payload}, ensure_ascii=False, default=_json_default) + "\n")

    def settings(self) -> BoardSettings:
        return self.inner.settings()

    def rubric_overrides(self) -> dict[str, RubricOverride]:
        return self.inner.rubric_overrides()

    def source_overrides(self) -> dict[str, SourceOverride]:
        return self.inner.source_overrides()

    def add_post(self, post: PostRecord) -> str:
        ref = f"dry-{uuid.uuid4().hex[:10]}"
        self._log("add_post", {"ref": ref, "post": post.model_dump()})
        return ref

    def update_post(self, ref: str, **fields: Any) -> None:
        self._log("update_post", {"ref": ref, "fields": fields})

    def posts_with_status(self, status: Status) -> list[PostRecord]:
        return self.inner.posts_with_status(status)

    def published_since(self, since: datetime) -> list[PostRecord]:
        return self.inner.published_since(since)

    def add_glossary(self, entry: GlossaryEntry) -> None:
        self._log("add_glossary", entry.model_dump())


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, BaseModel):
        return o.model_dump()
    return str(o)
