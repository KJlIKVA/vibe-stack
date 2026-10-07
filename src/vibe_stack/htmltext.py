"""HTML → текст без внешних зависимостей. Достаточно для статей, блогов и README."""

from __future__ import annotations

import re
from html.parser import HTMLParser

SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe", "template"}
BLOCK = {
    "p", "div", "section", "article", "main", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "pre", "blockquote", "tr", "table", "dt", "dd", "figcaption", "hr",
}
VOID = {"br", "hr", "img", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed", "track"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.chunks: list[str] = []
        self.main_chunks: list[str] = []
        self.main_depth = 0
        self.title = ""
        self._in_title = False
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
            return
        if tag == "time" and a.get("datetime"):
            self.meta.setdefault("time", a["datetime"])
        if tag in VOID:
            if tag in BLOCK:
                self._emit("\n")
            return
        if tag == "title":
            self._in_title = True
        if tag in SKIP:
            self.skip_depth += 1
        if tag in ("article", "main"):
            self.main_depth += 1
        if tag in BLOCK:
            self._emit("\n")
        if tag == "li":
            self._emit("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if tag == "title":
            self._in_title = False
        if tag in SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag in ("article", "main") and self.main_depth:
            self.main_depth -= 1
        if tag in BLOCK:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self.skip_depth:
            return
        self._emit(data)

    def _emit(self, s: str) -> None:
        self.chunks.append(s)
        if self.main_depth:
            self.main_chunks.append(s)


def _clean(s: str) -> str:
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def html_to_text(html: str) -> tuple[str, str, dict[str, str]]:
    """(заголовок, текст, meta). Если есть <article>/<main> с содержимым — берём его."""
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    main = _clean("".join(p.main_chunks))
    full = _clean("".join(p.chunks))
    text = main if len(main) >= 500 else full
    return _clean(p.title), text, p.meta


def published_from_meta(meta: dict[str, str]) -> str | None:
    for key in ("article:published_time", "og:published_time", "datepublished", "date", "pubdate", "time"):
        if meta.get(key):
            return meta[key]
    return None
