"""Список товаров из JSON-LD страницы каталога (решение 50): новые книги Manning.

У Manning нет RSS, но страница каталога (сортировка «newest» по умолчанию) отдаёт schema.org ItemList с 40
последними книгами: название, адрес, обложка. Даты в списке нет — свежесть решает дедуп «уже видели»,
а дату выхода (или начала раннего доступа MEAP) модель видит на странице книги.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..config import SourceConfig
from ..models import Candidate

log = logging.getLogger(__name__)
_LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def products(page_html: str) -> list[dict[str, Any]]:
    """Элементы ItemList (Product с name и url) из всех блоков JSON-LD страницы, в порядке страницы."""
    out: list[dict[str, Any]] = []
    for block in _LD_RE.findall(page_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "ItemList":
                continue
            for item in node.get("itemListElement") or []:
                if isinstance(item, dict) and item.get("@type") == "ListItem":
                    item = item.get("item") or {}
                if isinstance(item, dict) and item.get("name") and item.get("url"):
                    out.append(item)
    return out


class JsonLdListSource:
    def __init__(self, cfg: SourceConfig, client: httpx.Client) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.client = client

    def collect(self) -> list[Candidate]:
        assert self.cfg.url
        r = self.client.get(self.cfg.url, follow_redirects=True)
        r.raise_for_status()
        skip = [re.compile(p) for p in self.cfg.skip_title_regex]
        include = [re.compile(p, re.IGNORECASE) for p in self.cfg.include_title_regex]
        out = []
        for item in products(r.text)[:40]:
            title = str(item["name"]).strip()
            if any(p.search(title) for p in skip) or (include and not any(p.search(title) for p in include)):
                continue
            out.append(Candidate(
                source=self.name, source_type="jsonld_list", url=str(item["url"]), title=title,
                whitelist=self.cfg.whitelist, official_domains=self.cfg.official_domains, signal=0.5,
            ))
        return out
