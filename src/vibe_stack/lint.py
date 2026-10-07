"""Проверка готового поста кодом перед очередью и перед отправкой."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .guards import has_pipe_to_shell
from .urls import canonical_url, host_of, same_site

ALLOWED_TAGS = {"b", "i", "a", "code"}
VERIFIED_MARK = "✅ Сверено с первоисточником"
FORBIDDEN_MARKS = ("Запущено",)
HYPE_WORDS = ("революци", "game changer", "game-changer", "невероятн")
STANDARD_RUBRICS = {"tool", "skill_mcp", "trick", "case"}
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
# Telegram сам превращает в ссылки голые URL, домены и @упоминания — в обычном тексте их быть не должно
_AUTOLINK_TLDS = ("com|org|net|io|dev|ai|app|sh|xyz|ru|me|co|gg|tv|info|biz|site|online|link|click|top|so|to|ly|"
                  "pro|tech|cloud|store|shop|news|blog|page|fun|live|space|website|cc|us|uk|de")
_AUTOLINK_RE = re.compile(
    r"(?:https?://|www\.|tg://)\S+"
    rf"|\b[\w-]+(?:\.[\w-]+)*\.(?:{_AUTOLINK_TLDS})\b(?:/\S*)?"
    r"|(?<![\w@])@[A-Za-z][\w]{3,}",
    re.IGNORECASE,
)


@dataclass
class _Parsed:
    text: str = ""
    plain: str = ""  # текст вне <a> и <code>: здесь Telegram может создать ссылку сам
    links: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _TGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.out = _Parsed()
        self._chunks: list[str] = []
        self._plain: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.out.errors.append(f"bad_tag:{tag}")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self.out.links.append(href)
            if any(k != "href" for k, _ in attrs):
                self.out.errors.append("bad_attr:a")
        elif attrs:
            self.out.errors.append(f"bad_attr:{tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            self.out.errors.append(f"bad_tag:{tag}")
            return
        if not self.stack or self.stack[-1] != tag:
            self.out.errors.append(f"unbalanced:{tag}")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)
        if "a" not in self.stack and "code" not in self.stack:
            self._plain.append(data)

    def finish(self) -> _Parsed:
        self.close()
        if self.stack:
            self.out.errors.append("unclosed:" + ",".join(self.stack))
        self.out.text = "".join(self._chunks)
        self.out.plain = "".join(self._plain)
        return self.out


def parse_tg_html(post: str) -> _Parsed:
    p = _TGParser()
    p.feed(post)
    parsed = p.finish()
    # голые & и < в Telegram HTML ломают разметку
    if re.search(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)", post):
        parsed.errors.append("unescaped_amp")
    return parsed


def visible_length(post: str) -> int:
    return len(parse_tg_html(post).text)


def _numbers(text: str) -> set[str]:
    out = set()
    for n in _NUM_RE.findall(text):
        n = n.replace(",", ".")
        out.add(str(int(n)) if n.isdigit() else n)  # «07» и «7» — одно число (даты)
    return out


def lint_post(
    post: str,
    *,
    rubric: str,
    max_chars: int,
    source_url: str,
    allowed_text: str = "",
    template_text: str = "",
    weekly: bool = False,
) -> list[str]:
    """Список ошибок (пусто — пост годен).

    allowed_text — подтверждённые утверждения и заголовок: все числа в посте должны встречаться там
    (или в самом шаблоне рубрики, например «за 5 минут»).
    """
    errors: list[str] = []
    if not post.strip():
        return ["empty"]
    parsed = parse_tg_html(post)
    errors += parsed.errors
    if len(parsed.text) > max_chars:
        errors.append(f"too_long:{len(parsed.text)}>{max_chars}")
    if has_pipe_to_shell(html.unescape(post)):
        errors.append("unsafe_command")
    for mark in FORBIDDEN_MARKS:
        if mark in parsed.text:
            errors.append(f"forbidden_mark:{mark}")
    low = parsed.text.lower()
    for w in HYPE_WORDS:
        if w in low:
            errors.append(f"hype_word:{w}")

    if bare := _AUTOLINK_RE.findall(parsed.plain):
        errors.append("bare_link:" + ",".join(sorted(set(bare))[:3]))
    allowed = _numbers(allowed_text) | _numbers(template_text)
    stray = sorted(n for n in _numbers(parsed.text) - allowed if n not in {"0", "1", "2", "3"})
    if weekly:
        for href in parsed.links:
            if host_of(href) not in ("t.me", "telegram.me"):
                errors.append(f"weekly_link_not_telegram:{host_of(href)}")
        if stray:  # числа итогов считает код — модель не может их менять
            errors.append("unverified_numbers:" + ",".join(stray[:5]))
        return errors

    if len(parsed.links) != 1:
        errors.append(f"link_count:{len(parsed.links)}")
    src_host = host_of(source_url)
    src_canon = canonical_url(source_url) if source_url else ""
    for href in parsed.links:
        h = host_of(href)
        if not h or not (same_site(h, src_host) or same_site(src_host, h)):
            errors.append(f"link_domain:{h or href}")
        elif canonical_url(href) != src_canon:
            errors.append("link_not_source")  # тот же домен, но не та страница (например, другой репозиторий)
    if rubric in STANDARD_RUBRICS and VERIFIED_MARK not in parsed.text:
        errors.append("missing_verified_mark")
    if stray:
        errors.append("unverified_numbers:" + ",".join(stray[:5]))
    return errors
