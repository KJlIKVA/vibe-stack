"""Оперативная память системы: SQLite в ветке `state`.

Здесь хэши просмотренного, история кандидатов, опубликованные посты (для лимитов и планировщика),
запуски и расходы на LLM. То, что видит и правит человек, живёт на доске (Notion).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .timeutil import iso, parse_dt

# исходы-ошибки помним недолго: после них стоит попробовать снова, но не каждые 30 минут
SHORT_TTL = {"board_error": timedelta(hours=6), "llm_error": timedelta(hours=2)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key TEXT PRIMARY KEY,
    candidate_id TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    contour TEXT NOT NULL,
    source TEXT,
    url TEXT,
    domain TEXT,
    title TEXT,
    found_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL,
    rubric TEXT,
    score_total INTEGER,
    detail TEXT,
    PRIMARY KEY (id, run_id)
);
CREATE TABLE IF NOT EXISTS published (
    ref TEXT PRIMARY KEY,
    rubric TEXT NOT NULL,
    urgent INTEGER NOT NULL,
    title TEXT,
    source_url TEXT,
    domain TEXT,
    published_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    slot TEXT,
    tg_message_id INTEGER,
    counts_regular INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    contour TEXT NOT NULL,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step TEXT NOT NULL,
    model TEXT NOT NULL,
    ts TEXT NOT NULL,
    local_date TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS glossary (
    term TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    definition TEXT NOT NULL,
    published_at TEXT NOT NULL,
    post_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_date ON candidates(local_date);
CREATE INDEX IF NOT EXISTS idx_published_date ON published(local_date);
CREATE INDEX IF NOT EXISTS idx_llm_date ON llm_calls(local_date);
"""


@dataclass
class PublishedRow:
    ref: str
    rubric: str
    urgent: bool
    title: str
    source_url: str
    domain: str
    published_at: datetime
    local_date: date
    slot: str | None
    tg_message_id: int | None
    counts_regular: bool


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # --- seen / dedup ---------------------------------------------------------------------------
    def seen_outcome(self, keys: Iterable[str], now: datetime, ttl_days: int) -> str | None:
        """Исход по любому из ключей. Опубликованное помнится всегда, остальное — ttl_days."""
        keys = list(keys)
        if not keys:
            return None
        q = f"SELECT outcome, last_seen FROM seen WHERE key IN ({','.join('?' * len(keys))})"
        rows = self.db.execute(q, keys).fetchall()
        for r in rows:
            if r["outcome"] == "published":
                return "published"
        for r in rows:
            last = parse_dt(r["last_seen"])
            ttl = SHORT_TTL.get(r["outcome"], timedelta(days=ttl_days))
            if last and now - last < ttl:
                return r["outcome"]
        return None

    def has_key_outcome(self, keys: Iterable[str], outcome: str) -> bool:
        keys = list(keys)
        q = f"SELECT 1 FROM seen WHERE outcome=? AND key IN ({','.join('?' * len(keys))}) LIMIT 1"
        return self.db.execute(q, [outcome, *keys]).fetchone() is not None

    def mark_seen(self, keys: Iterable[str], candidate_id: str, outcome: str, now: datetime) -> None:
        ts = iso(now)
        for k in keys:
            row = self.db.execute("SELECT outcome FROM seen WHERE key=?", (k,)).fetchone()
            if row and row["outcome"] == "published" and outcome != "published":
                continue  # «опубликовано» не понижаем
            self.db.execute(
                "INSERT INTO seen(key, candidate_id, first_seen, last_seen, outcome) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen, outcome=excluded.outcome, "
                "candidate_id=excluded.candidate_id",
                (k, candidate_id, ts, ts, outcome),
            )
        self.db.commit()

    # --- candidates log ---------------------------------------------------------------------------
    def log_candidate(
        self, *, cid: str, run_id: str, contour: str, source: str, url: str, domain: str, title: str,
        now: datetime, day: date, stage: str, decision: str, reasons: list[str],
        rubric: str | None = None, score_total: int | None = None, detail: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, run_id, contour, source, url, domain, title, iso(now), day.isoformat(), stage, decision,
             json.dumps(reasons, ensure_ascii=False), rubric, score_total,
             json.dumps(detail, ensure_ascii=False, default=str) if detail else None),
        )
        self.db.commit()

    def candidates_between(self, start: date, end: date) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM candidates WHERE local_date >= ? AND local_date <= ? ORDER BY found_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    # --- published ---------------------------------------------------------------------------
    def record_published(
        self, *, ref: str, rubric: str, urgent: bool, title: str, source_url: str, domain: str,
        published_at: datetime, day: date, slot: str | None, tg_message_id: int | None,
        counts_regular: bool,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO published VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ref, rubric, int(urgent), title, source_url, domain, iso(published_at), day.isoformat(), slot,
             tg_message_id, int(counts_regular)),
        )
        self.db.commit()

    def _rows(self, rows: list[sqlite3.Row]) -> list[PublishedRow]:
        return [
            PublishedRow(
                ref=r["ref"], rubric=r["rubric"], urgent=bool(r["urgent"]), title=r["title"] or "",
                source_url=r["source_url"] or "", domain=r["domain"] or "",
                published_at=parse_dt(r["published_at"]),  # type: ignore[arg-type]
                local_date=date.fromisoformat(r["local_date"]), slot=r["slot"],
                tg_message_id=r["tg_message_id"], counts_regular=bool(r["counts_regular"]),
            )
            for r in rows
        ]

    def published_since(self, since: date) -> list[PublishedRow]:
        rows = self.db.execute(
            "SELECT * FROM published WHERE local_date >= ? ORDER BY published_at", (since.isoformat(),)
        ).fetchall()
        return self._rows(rows)

    def count_published(self, day: date, *, urgent: bool) -> int:
        if urgent:
            q = "SELECT COUNT(*) FROM published WHERE local_date=? AND urgent=1"
        else:
            q = "SELECT COUNT(*) FROM published WHERE local_date=? AND urgent=0 AND counts_regular=1"
        return self.db.execute(q, (day.isoformat(),)).fetchone()[0]

    def slot_filled(self, day: date, slot: str) -> bool:
        q = "SELECT 1 FROM published WHERE local_date=? AND slot=? LIMIT 1"
        return self.db.execute(q, (day.isoformat(), slot)).fetchone() is not None

    def last_regular(self) -> PublishedRow | None:
        rows = self.db.execute(
            "SELECT * FROM published WHERE urgent=0 AND counts_regular=1 ORDER BY published_at DESC LIMIT 1"
        ).fetchall()
        return self._rows(rows)[0] if rows else None

    def is_published_ref(self, ref: str) -> bool:
        return self.db.execute("SELECT 1 FROM published WHERE ref=?", (ref,)).fetchone() is not None

    # --- runs ---------------------------------------------------------------------------
    def start_run(self, run_id: str, contour: str, mode: str, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO runs(id, contour, mode, started_at) VALUES (?,?,?,?)", (run_id, contour, mode, iso(now))
        )
        self.db.commit()

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any], now: datetime) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
            (iso(now), status, json.dumps(summary, ensure_ascii=False, default=str), run_id),
        )
        self.db.commit()

    # --- llm usage ---------------------------------------------------------------------------
    def record_llm_call(
        self, *, run_id: str, step: str, model: str, now: datetime, day: date, input_tokens: int,
        cached_tokens: int, output_tokens: int, cost_usd: float, ok: bool,
    ) -> None:
        self.db.execute(
            "INSERT INTO llm_calls(run_id, step, model, ts, local_date, input_tokens, cached_tokens, "
            "output_tokens, cost_usd, ok) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, step, model, iso(now), day.isoformat(), input_tokens, cached_tokens, output_tokens,
             cost_usd, int(ok)),
        )
        self.db.commit()

    def llm_cost_on(self, day: date) -> float:
        q = "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE local_date=?"
        return float(self.db.execute(q, (day.isoformat(),)).fetchone()[0])

    def llm_tokens_since(self, model: str, since: datetime) -> int:
        """Токены модели (вход + выход) с момента since; ts в журнале — UTC ISO, сравнение строк корректно."""
        q = ("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_calls "
             "WHERE model=? AND ts >= ?")
        return int(self.db.execute(q, (model, iso(since))).fetchone()[0])

    def llm_calls_in_run(self, run_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM llm_calls WHERE run_id=?", (run_id,)).fetchone()[0]

    # --- словарь ---------------------------------------------------------------------------
    def add_glossary(self, *, term: str, source_url: str, definition: str, published_at: datetime,
                     post_url: str | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO glossary(term, source_url, definition, published_at, post_url) VALUES (?,?,?,?,?)",
            (term, source_url, definition, iso(published_at), post_url),
        )
        self.db.commit()

    def glossary_entries(self) -> list[sqlite3.Row]:
        """Опубликованные термины, новые первыми."""
        return self.db.execute("SELECT * FROM glossary ORDER BY published_at DESC").fetchall()

    # --- kv ---------------------------------------------------------------------------
    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()
