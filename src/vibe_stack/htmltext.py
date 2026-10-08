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


# --- картинки ---------------------------------------------------------------------------
_DIMS_RE = re.compile(r"-(\d{2,5})x(\d{2,5})\.(?:png|jpe?g|webp|gif)$", re.IGNORECASE)
_SKIP_ALT = ("logo", "icon", "avatar", "логотип", "иконк")
_SKIP_EXT = (".svg", ".ico")


class _Images(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.imgs: list[tuple[str, str]] = []  # (src, alt)
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image") and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag in ("nav", "footer", "header", "aside"):
            self.skip_depth += 1
        elif tag == "img" and not self.skip_depth:
            src = a.get("src") or a.get("data-src") or ""
            if src:
                self.imgs.append((src, a.get("alt", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("nav", "footer", "header", "aside") and self.skip_depth:
            self.skip_depth -= 1


def _image_url(src: str, base_url: str) -> str | None:
    """Абсолютный https-адрес картинки. Next.js отдаёт картинки через /_next/image?url=… — берём исходный адрес."""
    from urllib.parse import parse_qs, urljoin, urlsplit

    url = urljoin(base_url, src.strip())
    parts = urlsplit(url)
    if parts.path.endswith("/_next/image"):
        inner = parse_qs(parts.query).get("url", [""])[0]
        if not inner:
            return None
        url = urljoin(base_url, inner)
        parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.path.lower().endswith(_SKIP_EXT):
        return None
    if (m := _DIMS_RE.search(parts.path)) and int(m.group(1)) < 600:
        return None  # маленькая картинка (размер в имени файла, как у CDN Sanity): логотип, иконка
    return url


def page_images(html: str, base_url: str, limit: int = 12) -> tuple[str | None, list[str]]:
    """(главная картинка страницы — og:image/twitter:image, картинки статьи по порядку без логотипов)."""
    p = _Images()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    main = None
    for key in ("og:image:secure_url", "og:image", "og:image:url", "twitter:image"):
        if p.meta.get(key) and (main := _image_url(p.meta[key], base_url)):
            break
    figures: list[str] = []
    for src, alt in p.imgs:
        if any(w in alt.lower() for w in _SKIP_ALT):
            continue
        url = _image_url(src, base_url)
        if url and url != main and url not in figures:
            figures.append(url)
        if len(figures) >= limit:
            break
    return main, figures
