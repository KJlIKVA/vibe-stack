"""NotionBoard на фейковом клиенте: setup, запись поста, чтение статусов и настроек."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from vibe_stack.board import BoardUnavailable
from vibe_stack.models import PostRecord, Status
from vibe_stack.notion_board import NotionBoard


class FakeNotion:
    def __init__(self) -> None:
        self.children: list[dict[str, Any]] = []
        self.dbs: dict[str, dict[str, Any]] = {}  # db id → {title, ds}
        self.rows: dict[str, list[dict[str, Any]]] = {}  # ds id → pages
        self.blocks = SimpleNamespace(children=SimpleNamespace(list=self._children))
        self.databases = SimpleNamespace(retrieve=self._db_retrieve, create=self._db_create)
        self.data_sources = SimpleNamespace(query=self._query, retrieve=self._ds_retrieve, update=self._ds_update)
        self.pages = SimpleNamespace(create=self._page_create, update=self._page_update)

    def _children(self, block_id: str, start_cursor: str | None = None) -> dict[str, Any]:
        return {"results": self.children, "has_more": False}

    def _db_retrieve(self, database_id: str) -> dict[str, Any]:
        return {"id": database_id, "data_sources": [{"id": self.dbs[database_id]["ds"]}]}

    def _db_by_ds(self, ds_id: str) -> dict[str, Any]:
        return next(d for d in self.dbs.values() if d["ds"] == ds_id)

    def _ds_retrieve(self, data_source_id: str) -> dict[str, Any]:
        return {"id": data_source_id, "properties": dict(self._db_by_ds(data_source_id)["props"])}

    def _ds_update(self, data_source_id: str, properties) -> dict[str, Any]:
        self._db_by_ds(data_source_id)["props"].update(properties)
        return self._ds_retrieve(data_source_id)

    def _db_create(self, parent, title, initial_data_source) -> dict[str, Any]:
        db_id, ds_id = uuid.uuid4().hex, uuid.uuid4().hex
        name = title[0]["text"]["content"]
        self.dbs[db_id] = {"title": name, "ds": ds_id, "props": initial_data_source["properties"]}
        self.rows[ds_id] = []
        self.children.append({"id": db_id, "type": "child_database", "child_database": {"title": name}})
        return {"id": db_id, "data_sources": [{"id": ds_id}]}

    @staticmethod
    def _to_read(props: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in props.items():
            (typ, val), = v.items()
            if typ in ("title", "rich_text"):
                val = [{"plain_text": t["text"]["content"]} for t in val]
            out[k] = {"type": typ, typ: val}
        return out

    def _page_create(self, parent, properties) -> dict[str, Any]:
        page = {"id": uuid.uuid4().hex, "properties": self._to_read(properties)}
        self.rows[parent["data_source_id"]].append(page)
        return page

    def _page_update(self, page_id: str, properties) -> dict[str, Any]:
        for rows in self.rows.values():
            for p in rows:
                if p["id"] == page_id:
                    p["properties"].update(self._to_read(properties))
                    return p
        raise KeyError(page_id)

    def _query(self, data_source_id: str, filter=None, start_cursor=None) -> dict[str, Any]:
        rows = self.rows[data_source_id]
        for f in (filter.get("and", [filter]) if filter else []):
            if "select" in f:
                rows = [r for r in rows if (r["properties"].get(f["property"], {}).get("select") or {})
                        .get("name") == f["select"]["equals"]]
            elif "date" in f:
                rows = [r for r in rows if ((r["properties"].get(f["property"], {}).get("date") or {})
                                            .get("start") or "") >= f["date"]["on_or_after"]]
        return {"results": rows, "has_more": False}


@pytest.fixture
def board(cfg) -> tuple[NotionBoard, FakeNotion]:
    fake = FakeNotion()
    b = NotionBoard("t", "root", cfg, client=fake, sleep=lambda s: None)
    return b, fake


def test_setup_creates_all_databases_and_is_idempotent(board) -> None:
    b, fake = board
    created = b.setup()
    assert created == ["Posts", "Rubrics", "Sources", "Leaderboard history", "Настройки"]
    assert b.setup() == []
    posts_props = next(d for d in fake.dbs.values() if d["title"] == "Posts")["props"]
    assert {"Title", "Rubric", "Status", "Mode", "Urgent", "Score", "Scores", "Hard stops", "Reject reason",
            "Source URL", "Source domain", "Found at", "Published at", "TG message id", "Post HTML",
            "Verify"} <= set(posts_props)


def test_settings_start_paused(board, cfg) -> None:
    b, _ = board
    b.setup()
    s = b.settings()
    assert s.pause is True  # безопасный старт: канал на паузе, пока вы её не снимете
    assert s.publish_slots == cfg.schedule.publish_slots and s.tz == "Europe/Moscow"
    assert s.regular_per_day == cfg.limits.regular_per_day


def test_rubric_and_source_overrides(board) -> None:
    b, _ = board
    b.setup()
    rubrics = b.rubric_overrides()
    assert rubrics["analysis"].mode == "approve" and rubrics["tool"].mode == "auto"
    sources = b.source_overrides()
    assert sources["openai-news"].whitelist is True
    assert sources["anthropic-news"].enabled is True and sources["simon-willison"].whitelist is False


def test_post_roundtrip_and_status_filter(board, now) -> None:
    b, _ = board
    b.setup()
    long_html = "x" * 4500  # больше лимита одного text-объекта Notion
    ref = b.add_post(PostRecord(title="T", rubric="tool", status=Status.APPROVED, score=12,
                                scores={"novelty": 3}, source_url="https://a.dev", source_domain="a.dev",
                                found_at=now, html=long_html, verify={"verdict": "pass"}, candidate_id="c1"))
    got = b.posts_with_status(Status.APPROVED)
    assert len(got) == 1
    p = got[0]
    assert p.ref == ref and p.rubric == "tool" and p.html == long_html and p.verify == {"verdict": "pass"}
    b.update_post(ref, status=Status.PUBLISHED, tg_message_id=55, published_at=now)
    assert b.posts_with_status(Status.APPROVED) == []
    assert b.posts_with_status(Status.PUBLISHED)[0].tg_message_id == 55


def test_missing_database_is_board_unavailable(board) -> None:
    b, _ = board
    with pytest.raises(BoardUnavailable):
        b.settings()


def test_published_since(board, now) -> None:
    from datetime import timedelta

    b, _ = board
    b.setup()
    old = b.add_post(PostRecord(title="old", rubric="tool", status=Status.APPROVED, html="x"))
    new = b.add_post(PostRecord(title="new", rubric="tool", status=Status.APPROVED, html="x"))
    b.update_post(old, status=Status.PUBLISHED, published_at=now - timedelta(days=2))
    b.update_post(new, status=Status.PUBLISHED, published_at=now - timedelta(hours=1))
    assert [p.title for p in b.published_since(now - timedelta(hours=12))] == ["new"]


def test_settings_fail_closed(board) -> None:
    b, fake = board
    b.setup()
    ds = b.data_sources()["Настройки"]
    row = fake.rows[ds][0]
    # вторая строка без паузы не отменяет паузу первой
    fake.rows[ds].append({"id": "x", "properties": {**row["properties"], "Пауза": {"type": "checkbox",
                                                                                     "checkbox": False}}})
    assert b.settings().pause is True
    # переименованный флажок — считаем, что пауза включена (BoardUnavailable)
    for r in fake.rows[ds]:
        r["properties"].pop("Пауза")
    with pytest.raises(BoardUnavailable):
        b.settings()


def test_create_not_retried_after_timeout(cfg) -> None:
    from notion_client.errors import RequestTimeoutError

    fake = FakeNotion()
    b = NotionBoard("t", "root", cfg, client=fake, sleep=lambda s: None)
    b.setup()
    calls = []

    def boom(**kw):
        calls.append(1)
        raise RequestTimeoutError()

    fake.pages.create = boom
    with pytest.raises(BoardUnavailable):
        b.add_post(PostRecord(title="t", rubric="tool", status=Status.APPROVED, html="x"))
    assert len(calls) == 1  # строка могла создаться — повтор дал бы дубль


def test_setup_adds_new_columns_to_existing_databases(board) -> None:
    b, fake = board
    b.setup()
    posts = next(d for d in fake.dbs.values() if d["title"] == "Posts")
    del posts["props"]["Время публикации"]  # база создана старой версией
    b._ds = None
    assert b.setup() == []
    assert "Время публикации" in posts["props"]


def test_planned_time_roundtrip_in_local_tz(board, now) -> None:
    b, fake = board
    b.setup()
    ref = b.add_post(PostRecord(title="T", rubric="tool", status=Status.APPROVED, html="x"))
    b.update_post(ref, planned_at=now)
    row = next(r for rows in fake.rows.values() for r in rows if r["id"] == ref)
    assert row["properties"]["Время публикации"]["date"]["start"].endswith("+03:00")
    (p,) = b.posts_with_status(Status.APPROVED)
    assert p.planned_at == now

