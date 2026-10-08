"""Контекст одного запуска контура: конфиг, состояние, доска, модель, Telegram, режим."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .board import Board, BoardSettings
from .config import Config, Rubric, SourceConfig
from .fetch import Fetcher
from .llm import LLM
from .models import Candidate
from .storage import State
from .telegram import DryRunTelegram, Notifier, Telegram
from .timeutil import Clock, local, local_date

log = logging.getLogger(__name__)
APPROVE_ONLY = ("analysis",)  # рубрики, которые нельзя перевести в авто ни конфигом, ни из Notion

SourcesFactory = Callable[[list[SourceConfig]], list[Any]]


@dataclass
class Runtime:
    cfg: Config
    state: State
    board: Board
    tg: Telegram | DryRunTelegram
    notifier: Notifier
    llm: LLM
    fetcher: Fetcher
    clock: Clock
    run_id: str
    out_dir: Path
    mode: str  # "dry-run" | "publish"
    channel_id: str
    sources_factory: SourcesFactory
    fixture_candidates: list[Candidate] | None = None
    force: bool = False  # --force: пропустить защиту «уже запускался сегодня/на этой неделе»
    registry: Any = None  # проверка пакетов в PyPI/npm для песочницы (sandbox.Registry) или None
    _settings: BoardSettings | None = field(default=None, repr=False)

    # --- настройки с учётом доски ---------------------------------------------------------------------------
    def settings(self) -> BoardSettings:
        """Читает настройки доски (бросает BoardUnavailable). Кэшируется на запуск."""
        if self._settings is None:
            self._settings = self.board.settings()
        return self._settings

    @property
    def tz(self) -> str:
        s = self._settings
        return (s.tz if s and s.tz else None) or self.cfg.channel.tz

    def now(self) -> datetime:
        return self.clock()

    def today(self) -> date:
        return local_date(self.clock(), self.tz)

    def now_local(self) -> datetime:
        return local(self.clock(), self.tz)

    def rubrics(self) -> dict[str, Rubric]:
        """Рубрики из конфига с поправками из таблицы «Рубрики»."""
        out = dict(self.cfg.rubrics)
        try:
            overrides = self.board.rubric_overrides()
        except Exception as e:
            log.warning("не прочитать рубрики с доски, беру config.yaml: %s", e)
            overrides = {}
        for key, ov in overrides.items():
            if key in out:
                upd = {k: v for k, v in (("mode", ov.mode), ("enabled", ov.enabled)) if v is not None}
                out[key] = out[key].model_copy(update=upd)
        for key in APPROVE_ONLY:  # раздел 6: «Разбор» выходит только после вашего одобрения, Notion это не меняет
            if key in out and out[key].mode != "approve":
                log.warning("рубрика %s: режим %s не допускается — только approve", key, out[key].mode)
                out[key] = out[key].model_copy(update={"mode": "approve"})
        return out

    def sources(self, contour: str) -> list[Any]:
        if self.fixture_candidates is not None:
            return [_FixtureSource(self.fixture_candidates, contour)]
        try:
            overrides = self.board.source_overrides()
        except Exception as e:
            log.warning("не прочитать источники с доски, беру config.yaml: %s", e)
            overrides = {}
        chosen = []
        for s in self.cfg.sources:
            ov = overrides.get(s.name)
            if ov is not None:
                s = s.model_copy(update={k: v for k, v in (("enabled", ov.enabled), ("whitelist", ov.whitelist))
                                         if v is not None})
            if not s.enabled or contour not in s.contours:
                continue
            if contour == "urgent" and not s.whitelist:
                continue
            chosen.append(s)
        return self.sources_factory(chosen)

    def post_link(self, message_id: int | None) -> str | None:
        """Ссылка на пост в канале (для итогов недели и подборки «Что посмотреть»)."""
        if not message_id or message_id < 0:
            return None
        if self.cfg.channel.username:
            return f"https://t.me/{self.cfg.channel.username.lstrip('@')}/{message_id}"
        cid = self.channel_id.removeprefix("-100")
        return f"https://t.me/c/{cid}/{message_id}" if cid.isdigit() else None

    # --- «один раз за период» ---------------------------------------------------------------------------
    def already_done(self, key: str) -> bool:
        """Только для --publish: dry-run и --force эту защиту не проверяют."""
        return self.mode == "publish" and not self.force and self.state.get(f"done:{key}") is not None

    def mark_done(self, key: str) -> None:
        if self.mode == "publish":
            self.state.put(f"done:{key}", self.run_id)

    # --- вывод ---------------------------------------------------------------------------
    def write_out(self, rel: str, content: str) -> Path:
        p = self.out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def decision(self, contour: str, c: Candidate, stage: str, decision: str, reasons: list[str],
                 rubric: str | None = None, score_total: int | None = None,
                 detail: dict[str, Any] | None = None) -> None:
        now = self.clock()
        self.state.log_candidate(
            cid=c.id, run_id=self.run_id, contour=contour, source=c.source, url=c.url, domain=c.domain,
            title=c.title, now=now, day=local_date(now, self.tz), stage=stage, decision=decision, reasons=reasons,
            rubric=rubric, score_total=score_total, detail=detail,
        )
        rec = {"id": c.id, "title": c.title, "url": c.url, "stage": stage, "decision": decision,
               "reasons": reasons, "rubric": rubric, "score": score_total}
        with (self.out_dir / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.info("%-8s %-17s %-10s %s %s", contour, decision, stage, c.title[:70], ",".join(reasons))


class _FixtureSource:
    def __init__(self, candidates: list[Candidate], contour: str) -> None:
        self.name = f"fixtures:{contour}"
        self.candidates = candidates
        self.contour = contour

    def collect(self) -> list[Candidate]:
        if self.contour == "urgent":
            return [c for c in self.candidates if c.whitelist]
        return list(self.candidates)
