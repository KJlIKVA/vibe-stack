"""Публичная страница словаря на Telegraph (telegra.ph/api).

Страница создаётся один раз (`vibe-stack telegraph-setup`), дальше editPage по тому же path: ссылка постоянная.
Контент пересобирается целиком из таблицы glossary. Токен — секрет TELEGRAPH_TOKEN.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegra.ph"
TITLE = "Словарь Vibe Stack"
AUTHOR = "Vibe Stack"
MAX_BYTES = 60_000  # лимит Telegraph — 64 KB на content, оставляем запас
MAX_DEFINITION = 600  # на странице определение короче, чем в Notion: так влезает больше терминов
INTRO = ("Термины из рубрики «📖 Слово дня» канала Vibe Stack. Каждое определение сверено с источником; "
         "по ссылке «Источник» — первоисточник, по ссылке «Пост» — разбор в канале.")


class TelegraphError(RuntimeError):
    pass


def _nodes(entries: list[Any]) -> list[Any]:
    nodes: list[Any] = [{"tag": "p", "children": [INTRO]}]
    for e in sorted(entries, key=lambda e: str(e["term"]).lower()):
        links: list[Any] = [{"tag": "a", "attrs": {"href": e["source_url"]}, "children": ["Источник"]}]
        if e["post_url"]:
            links += [" · ", {"tag": "a", "attrs": {"href": e["post_url"]}, "children": ["Пост"]}]
        definition = str(e["definition"])
        if len(definition) > MAX_DEFINITION:
            definition = definition[:MAX_DEFINITION].rsplit(" ", 1)[0] + "…"
        nodes += [
            {"tag": "h4", "children": [str(e["term"])]},
            {"tag": "p", "children": [definition]},
            {"tag": "p", "children": links},
        ]
    return nodes


def _size(nodes: list[Any]) -> int:
    return len(json.dumps(nodes, ensure_ascii=False).encode())


def build_nodes(entries: list[Any]) -> tuple[list[Any], int]:
    """(Node-массив Telegraph, сколько терминов не поместилось). h4 — термин, p — определение, p — ссылки.

    Если словарь не влезает в лимит Telegraph, со страницы уходят самые старые термины (в Notion они остаются).
    """
    keep = sorted(entries, key=lambda e: str(e["published_at"] or ""), reverse=True)  # новые первыми
    nodes = _nodes(keep)
    while _size(nodes) > MAX_BYTES and keep:
        keep = keep[:-1]
        nodes = _nodes(keep)
    dropped = len(entries) - len(keep)
    if dropped:
        log.warning("словарь не помещается в 64 KB Telegraph — не показаны %d старых терминов", dropped)
    return nodes, dropped


class TelegraphPage:
    def __init__(self, token: str, path: str, author_url: str = "", client: httpx.Client | None = None) -> None:
        self.token = token
        self.path = path
        self.author_url = author_url
        self.client = client or httpx.Client(timeout=30)

    def _call(self, method: str, data: dict[str, Any]) -> Any:
        r = self.client.post(f"{API}/{method}", data=data)
        body = r.json()
        if not body.get("ok"):
            raise TelegraphError(f"{method}: {body.get('error')}")  # в ошибке нет токена
        return body["result"]

    def sync(self, entries: list[Any]) -> tuple[str, int]:
        """(адрес страницы, сколько терминов не поместилось)."""
        nodes, dropped = build_nodes(entries)
        result = self._call(f"editPage/{self.path}", {
            "access_token": self.token, "title": TITLE, "author_name": AUTHOR, "author_url": self.author_url,
            "content": json.dumps(nodes, ensure_ascii=False),
        })
        return result["url"], dropped


class DryRunPage:
    """dry-run: вместо Telegraph пишет контент страницы в out/."""

    def __init__(self, out: Path) -> None:
        self.out = out

    def sync(self, entries: list[Any]) -> tuple[str, int]:
        nodes, dropped = build_nodes(entries)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(json.dumps(nodes, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"file://{self.out}", dropped


def setup(author_url: str, client: httpx.Client | None = None) -> tuple[str, str, str]:
    """Один раз: аккаунт и пустая страница. Возвращает (token, path, url)."""
    client = client or httpx.Client(timeout=30)

    def call(method: str, data: dict[str, Any]) -> Any:
        body = client.post(f"{API}/{method}", data=data).json()
        if not body.get("ok"):
            raise TelegraphError(f"{method}: {body.get('error')}")
        return body["result"]

    acc = call("createAccount", {"short_name": "VibeStack", "author_name": AUTHOR, "author_url": author_url})
    token = acc["access_token"]
    page = call("createPage", {"access_token": token, "title": TITLE, "author_name": AUTHOR,
                               "author_url": author_url,
                               "content": json.dumps([{"tag": "p", "children": [INTRO]}], ensure_ascii=False)})
    return token, page["path"], page["url"]
