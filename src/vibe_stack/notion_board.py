"""Доска в Notion (API 2025-09-03: база → data source).

Нужны только NOTION_TOKEN и NOTION_ROOT_PAGE_ID: базы находятся по названию среди дочерних
блоков корневой страницы «Vibe Stack». Создаёт их команда `vibe-stack notion-setup`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from .board import (
    BoardSettings,
    BoardUnavailable,
    GlossaryEntry,
    RubricOverride,
    SourceOverride,
    validate_settings,
)
from .config import Config
from .models import HARD_STOPS, PostRecord, Status
from .timeutil import iso, parse_dt

log = logging.getLogger(__name__)
T = TypeVar("T")

DB_POSTS = "Posts"
DB_RUBRICS = "Rubrics"
DB_SOURCES = "Sources"
DB_GLOSSARY = "Glossary"
DB_LEADERBOARD = "Leaderboard history"
DB_SETTINGS = "Настройки"
MODE_TO_NOTION = {"auto": "авто", "approve": "approve"}
MODE_FROM_NOTION = {v: k for k, v in MODE_TO_NOTION.items()}
CHUNK = 1900  # лимит Notion — 2000 символов на один text-объект


def _text(value: str) -> list[dict[str, Any]]:
    value = value or ""
    return [{"type": "text", "text": {"content": value[i:i + CHUNK]}} for i in range(0, len(value), CHUNK)][:100]


def _plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    items = prop.get(prop.get("type", ""), []) or []
    if isinstance(items, list):
        return "".join(i.get("plain_text") or i.get("text", {}).get("content", "") for i in items)
    return ""


def _select(prop: dict[str, Any] | None) -> str | None:
    sel = (prop or {}).get("select")
    return sel.get("name") if sel else None


def _number(prop: dict[str, Any] | None) -> float | None:
    return (prop or {}).get("number")


def _checkbox(prop: dict[str, Any] | None) -> bool | None:
    return (prop or {}).get("checkbox")


def _date(prop: dict[str, Any] | None) -> datetime | None:
    d = (prop or {}).get("date")
    return parse_dt(d.get("start")) if d else None


def posts_schema(cfg: Config) -> dict[str, Any]:
    return {
        "Title": {"title": {}},
        "Rubric": {"select": {"options": [{"name": r.title} for r in cfg.rubrics.values()]}},
        "Status": {"select": {"options": [{"name": s.value} for s in Status]}},
        "Mode": {"select": {"options": [{"name": "авто"}, {"name": "approve"}]}},
        "Urgent": {"checkbox": {}},
        "Score": {"number": {}},
        "Scores": {"rich_text": {}},
        "Hard stops": {"multi_select": {"options": [{"name": h} for h in HARD_STOPS]}},
        "Reject reason": {"rich_text": {}},
        "Source URL": {"url": {}},
        "Source domain": {"rich_text": {}},
        "Found at": {"date": {}},
        "Published at": {"date": {}},
        "TG message id": {"number": {}},
        "Post HTML": {"rich_text": {}},
        "Verify": {"rich_text": {}},
        "Candidate ID": {"rich_text": {}},
    }


SCHEMAS: dict[str, Callable[[Config], dict[str, Any]]] = {
    DB_POSTS: posts_schema,
    DB_RUBRICS: lambda cfg: {
        "Name": {"title": {}},
        "Key": {"rich_text": {}},
        "Режим": {"select": {"options": [{"name": "авто"}, {"name": "approve"}]}},
        "Недельная квота": {"number": {}},
        "Включена": {"checkbox": {}},
    },
    DB_SOURCES: lambda cfg: {
        "Name": {"title": {}},
        "URL": {"url": {}},
        "Тип": {"select": {"options": [{"name": t} for t in
                                       ("rss", "github_releases", "github_search", "hackernews", "sitemap")]}},
        "Белый список": {"checkbox": {}},
        "Включён": {"checkbox": {}},
    },
    DB_GLOSSARY: lambda cfg: {
        "Термин": {"title": {}},
        "Определение": {"rich_text": {}},
        "Источник": {"url": {}},
        "Дата публикации": {"date": {}},
        "Ссылка на пост": {"url": {}},
    },
    DB_LEADERBOARD: lambda cfg: {
        "Запись": {"title": {}},
        "Дата": {"date": {}},
        "Источник рейтинга": {"select": {"options": [{"name": "Arena · текст"}, {"name": "Arena · webdev"},
                                                     {"name": "Artificial Analysis"}]}},
        "Топ-3": {"rich_text": {}},
        "Ссылка на данные": {"url": {}},
    },
    DB_SETTINGS: lambda cfg: {
        "Name": {"title": {}},
        "Пауза": {"checkbox": {}},
        "Лимит обычных": {"number": {}},
        "Лимит срочных": {"number": {}},
        "Время публикаций": {"rich_text": {}},
        "TZ": {"rich_text": {}},
    },
}


class NotionBoard:
    def __init__(self, token: str, root_page_id: str, cfg: Config, client: Any | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if client is None:
            from notion_client import Client

            client = Client(auth=token)
        self.client = client
        self.root = root_page_id
        self.cfg = cfg
        self.sleep = sleep
        self._ds: dict[str, str] | None = None
        self._rubric_by_title = {r.title: k for k, r in cfg.rubrics.items()}

    # --- инфраструктура ---------------------------------------------------------------------------
    def _retry(self, fn: Callable[[], T], *, idempotent: bool = True) -> T:
        """Повторы на временных ошибках. Неидемпотентное создание повторяем только при rate_limited:
        после таймаута или 5xx строка могла уже создаться, повтор дал бы дубль."""
        from notion_client.errors import APIResponseError, HTTPResponseError, RequestTimeoutError

        for attempt in range(4):
            try:
                return fn()
            except (APIResponseError, HTTPResponseError, RequestTimeoutError) as e:
                status = getattr(e, "status", None)
                code = getattr(e, "code", "")
                if code == "rate_limited":
                    transient = True
                else:
                    transient = idempotent and ((status is not None and status >= 500)
                                                or isinstance(e, RequestTimeoutError))
                if not transient or attempt == 3:
                    raise
                self.sleep(2.0 * (attempt + 1))
        raise AssertionError("unreachable")

    def _write(self, what: str, fn: Callable[[], T], *, idempotent: bool) -> T:
        try:
            return self._retry(fn, idempotent=idempotent)
        except BoardUnavailable:
            raise
        except Exception as e:
            raise BoardUnavailable(f"Notion: не удалось {what}: {type(e).__name__}: {str(e)[:200]}") from e

    def _paginate(self, fn: Callable[..., dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self._retry(lambda c=cursor: fn(**kwargs, **({"start_cursor": c} if c else {})))
            out.extend(page.get("results", []))
            if not page.get("has_more"):
                return out
            cursor = page.get("next_cursor")

    def data_sources(self) -> dict[str, str]:
        """Название базы → id её data source (ищем среди дочерних блоков корневой страницы)."""
        if self._ds is not None:
            return self._ds
        try:
            blocks = self._paginate(self.client.blocks.children.list, block_id=self.root)
            found: dict[str, str] = {}
            for b in blocks:
                if b.get("type") != "child_database":
                    continue
                title = b["child_database"].get("title", "")
                db = self._retry(lambda bid=b["id"]: self.client.databases.retrieve(database_id=bid))
                sources = db.get("data_sources") or []
                if sources:
                    found[title] = sources[0]["id"]
        except Exception as e:
            raise BoardUnavailable(f"Notion недоступен: {type(e).__name__}: {str(e)[:200]}") from e
        self._ds = found
        return found

    def _ds_id(self, name: str) -> str:
        ds = self.data_sources().get(name)
        if not ds:
            raise BoardUnavailable(f"в Notion нет базы «{name}» — запустите `vibe-stack notion-setup`")
        return ds

    def _query(self, name: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            return self._paginate(self.client.data_sources.query, data_source_id=self._ds_id(name), **kwargs)
        except BoardUnavailable:
            raise
        except Exception as e:
            raise BoardUnavailable(f"Notion: не прочитать «{name}»: {type(e).__name__}") from e

    # --- чтение ---------------------------------------------------------------------------
    def settings(self) -> BoardSettings:
        rows = self._query(DB_SETTINGS)
        if not rows:
            raise BoardUnavailable("в базе «Настройки» нет строки")
        if any("Пауза" not in r["properties"] for r in rows):
            raise BoardUnavailable("в «Настройках» нет флажка «Пауза» — считаю, что пауза включена")
        # строк может оказаться несколько: пауза включена, если она стоит хотя бы в одной
        paused = any(_checkbox(r["properties"].get("Пауза")) for r in rows)
        if len(rows) > 1:
            log.warning("в «Настройках» %d строк — значения беру из первой, паузу — из любой", len(rows))
        p = rows[0]["properties"]
        slots = [s.strip() for s in _plain(p.get("Время публикаций")).replace(";", ",").split(",") if s.strip()]
        reg, urg = _number(p.get("Лимит обычных")), _number(p.get("Лимит срочных"))
        return validate_settings(BoardSettings(
            pause=paused,
            regular_per_day=int(reg) if reg is not None else None,
            urgent_per_day=int(urg) if urg is not None else None,
            publish_slots=slots or None,
            tz=_plain(p.get("TZ")).strip() or None,
        ))

    def rubric_overrides(self) -> dict[str, RubricOverride]:
        out = {}
        for row in self._query(DB_RUBRICS):
            p = row["properties"]
            key = _plain(p.get("Key")).strip() or self._rubric_by_title.get(_plain(p.get("Name")).strip())
            if not key:
                continue
            mode = _select(p.get("Режим"))
            quota = _number(p.get("Недельная квота"))
            out[key] = RubricOverride(
                mode=MODE_FROM_NOTION.get(mode or ""),
                enabled=_checkbox(p.get("Включена")),
                weekly_quota=int(quota) if quota is not None else None,
            )
        return out

    def source_overrides(self) -> dict[str, SourceOverride]:
        out = {}
        for row in self._query(DB_SOURCES):
            p = row["properties"]
            name = _plain(p.get("Name")).strip()
            if name:
                out[name] = SourceOverride(whitelist=_checkbox(p.get("Белый список")),
                                           enabled=_checkbox(p.get("Включён")))
        return out

    def posts_with_status(self, status: Status) -> list[PostRecord]:
        rows = self._query(DB_POSTS, filter={"property": "Status", "select": {"equals": status.value}})
        return [self._to_record(r) for r in rows]

    def published_since(self, since: datetime) -> list[PostRecord]:
        rows = self._query(DB_POSTS, filter={"and": [
            {"property": "Status", "select": {"equals": Status.PUBLISHED.value}},
            {"property": "Published at", "date": {"on_or_after": iso(since)}},
        ]})
        return [p for p in (self._to_record(r) for r in rows) if p.published_at and p.published_at >= since]

    def _to_record(self, row: dict[str, Any]) -> PostRecord:
        p = row["properties"]
        rubric_title = _select(p.get("Rubric")) or ""
        scores_raw = _plain(p.get("Scores"))
        verify_raw = _plain(p.get("Verify"))
        score = _number(p.get("Score"))
        mid = _number(p.get("TG message id"))
        return PostRecord(
            ref=row["id"],
            title=_plain(p.get("Title")),
            rubric=self._rubric_by_title.get(rubric_title, rubric_title),
            status=Status(_select(p.get("Status")) or Status.DRAFT.value),
            mode=MODE_FROM_NOTION.get(_select(p.get("Mode")) or "", "auto"),  # type: ignore[arg-type]
            urgent=bool(_checkbox(p.get("Urgent"))),
            score=int(score) if score is not None else None,
            scores=_safe_json(scores_raw),
            hard_stops=[o["name"] for o in (p.get("Hard stops") or {}).get("multi_select", [])],
            reject_reason=_plain(p.get("Reject reason")),
            source_url=(p.get("Source URL") or {}).get("url") or "",
            source_domain=_plain(p.get("Source domain")),
            found_at=_date(p.get("Found at")),
            published_at=_date(p.get("Published at")),
            tg_message_id=int(mid) if mid is not None else None,
            html=_plain(p.get("Post HTML")),
            verify=_safe_json(verify_raw),
            candidate_id=_plain(p.get("Candidate ID")) or None,
        )

    # --- запись ---------------------------------------------------------------------------
    def _props(self, fields: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in fields.items():
            match k:
                case "title":
                    out["Title"] = {"title": _text(v[:200])}
                case "rubric":
                    out["Rubric"] = {"select": {"name": self.cfg.rubrics[v].title if v in self.cfg.rubrics else v}}
                case "status":
                    out["Status"] = {"select": {"name": Status(v).value}}
                case "mode":
                    out["Mode"] = {"select": {"name": MODE_TO_NOTION[v]}}
                case "urgent":
                    out["Urgent"] = {"checkbox": bool(v)}
                case "score":
                    out["Score"] = {"number": v}
                case "scores":
                    out["Scores"] = {"rich_text": _text(json.dumps(v, ensure_ascii=False) if v else "")}
                case "hard_stops":
                    out["Hard stops"] = {"multi_select": [{"name": h} for h in v]}
                case "reject_reason":
                    out["Reject reason"] = {"rich_text": _text(v)}
                case "source_url":
                    out["Source URL"] = {"url": v or None}
                case "source_domain":
                    out["Source domain"] = {"rich_text": _text(v)}
                case "found_at":
                    out["Found at"] = {"date": {"start": iso(v)} if v else None}
                case "published_at":
                    out["Published at"] = {"date": {"start": iso(v)} if v else None}
                case "tg_message_id":
                    out["TG message id"] = {"number": v}
                case "html":
                    out["Post HTML"] = {"rich_text": _text(v)}
                case "verify":
                    out["Verify"] = {"rich_text": _text(json.dumps(v, ensure_ascii=False) if v else "")}
                case "candidate_id":
                    out["Candidate ID"] = {"rich_text": _text(v or "")}
                case "ref":
                    pass
                case _:
                    raise KeyError(f"неизвестное поле поста: {k}")
        return out

    def add_post(self, post: PostRecord) -> str:
        props = self._props(post.model_dump(exclude={"ref"}))
        ds = self._ds_id(DB_POSTS)
        page = self._write("создать строку поста", lambda: self.client.pages.create(
            parent={"type": "data_source_id", "data_source_id": ds}, properties=props), idempotent=False)
        return page["id"]

    def update_post(self, ref: str, **fields: Any) -> None:
        props = self._props(fields)
        self._write("обновить строку поста", lambda: self.client.pages.update(page_id=ref, properties=props),
                    idempotent=True)

    def add_glossary(self, entry: GlossaryEntry) -> None:
        ds = self._ds_id(DB_GLOSSARY)
        props = {
            "Термин": {"title": _text(entry.term)},
            "Определение": {"rich_text": _text(entry.definition)},
            "Источник": {"url": entry.source_url},
            "Дата публикации": {"date": {"start": iso(entry.published_at)}},
            "Ссылка на пост": {"url": entry.post_url},
        }
        self._write("добавить термин в словарь", lambda: self.client.pages.create(
            parent={"type": "data_source_id", "data_source_id": ds}, properties=props), idempotent=False)

    def add_leaderboard_row(self, snap: Any) -> None:
        ds = self._ds_id(DB_LEADERBOARD)
        props = {
            "Запись": {"title": _text(f"{snap.label} · {snap.date}")},
            "Дата": {"date": {"start": snap.date}},
            "Источник рейтинга": {"select": {"name": snap.label}},
            "Топ-3": {"rich_text": _text(" · ".join(f"{i}. {m}" for i, m in enumerate(snap.top[:3], 1)))},
            "Ссылка на данные": {"url": snap.data_url},
        }
        self._write("записать историю рейтинга", lambda: self.client.pages.create(
            parent={"type": "data_source_id", "data_source_id": ds}, properties=props), idempotent=False)

    # --- первичная настройка ---------------------------------------------------------------------------
    def setup(self) -> list[str]:
        """Создаёт недостающие базы под корневой страницей и заполняет справочники. Повторный запуск безопасен."""
        self._ds = None
        existing = self.data_sources()
        created = []
        for name, schema_fn in SCHEMAS.items():
            if name in existing:
                continue
            db = self._retry(lambda n=name, f=schema_fn: self.client.databases.create(
                parent={"type": "page_id", "page_id": self.root},
                title=[{"type": "text", "text": {"content": n}}],
                initial_data_source={"properties": f(self.cfg)},
            ))
            existing[name] = db["data_sources"][0]["id"]
            created.append(name)
            if name == DB_RUBRICS:
                for key, r in self.cfg.rubrics.items():
                    self._create_row(name, {
                        "Name": {"title": _text(r.title)}, "Key": {"rich_text": _text(key)},
                        "Режим": {"select": {"name": MODE_TO_NOTION[r.mode]}},
                        "Недельная квота": {"number": self.cfg.planner.weekly_min.get(key)
                                            or self.cfg.planner.weekly_max.get(key)},
                        "Включена": {"checkbox": r.enabled},
                    })
            elif name == DB_SOURCES:
                for s in self.cfg.sources:
                    url = s.url or (f"https://github.com/{s.repo}" if s.repo else None)
                    self._create_row(name, {
                        "Name": {"title": _text(s.name)}, "URL": {"url": url}, "Тип": {"select": {"name": s.type}},
                        "Белый список": {"checkbox": s.whitelist}, "Включён": {"checkbox": s.enabled},
                    })
            elif name == DB_SETTINGS:
                self._create_row(name, {
                    "Name": {"title": _text("Настройки")}, "Пауза": {"checkbox": True},
                    "Лимит обычных": {"number": self.cfg.limits.regular_per_day},
                    "Лимит срочных": {"number": self.cfg.limits.urgent_per_day},
                    "Время публикаций": {"rich_text": _text(", ".join(self.cfg.schedule.publish_slots))},
                    "TZ": {"rich_text": _text(self.cfg.channel.tz)},
                })
        self._ds = existing
        return created

    def _create_row(self, db_name: str, props: dict[str, Any]) -> None:
        self._retry(lambda: self.client.pages.create(
            parent={"type": "data_source_id", "data_source_id": self.data_sources()[db_name]}, properties=props))


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
