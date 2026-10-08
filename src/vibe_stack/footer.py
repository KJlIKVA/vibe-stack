"""Подвал поста собирает код (решение 47), а не модель:

    …текст поста…

    Сверено с <a href="URL первоисточника">первоисточником</a> ✅

    #хештег

Модель пишет пост по шаблону ТЗ (там подвал свой), код срезает его хвост — строку ссылки, «Сверено», хештеги —
и ставит единый подвал. Так ссылка всегда ведёт на первоисточник, а формат одинаковый у всех рубрик.
При отправке подвал пересобирается ещё раз: посты, написанные в старом формате, выходят в новом.
"""

from __future__ import annotations

import html
import re

from .config import Rubric

CHECK = "✅"
_LINK_RE = re.compile(r"<a\s[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HASHTAG_RE = re.compile(r"#[\w]+")


def footer(source_url: str, hashtags: list[str]) -> str:
    link = f'<a href="{html.escape(source_url, quote=True)}">первоисточником</a>'
    return f"Сверено с {link} {CHECK}\n\n" + " ".join(hashtags)


def allowed_hashtags(rubric: Rubric, template: str = "") -> list[str]:
    """Хештеги рубрики: из настроек и из шаблона ТЗ (у «Книги/видео» — #книга или #видео)."""
    out = [rubric.hashtag] if rubric.hashtag else []
    for tag in _HASHTAG_RE.findall(template):
        if tag != "#рубрика" and tag not in out:
            out.append(tag)
    return out


def _is_footer_line(line: str) -> bool:
    """Строка подвала от модели: ссылка на источник с короткой подписью, «Сверено», хештеги."""
    plain = _TAG_RE.sub("", _LINK_RE.sub("", line))
    plain = html.unescape(plain)
    rest = _HASHTAG_RE.sub("", plain).replace("·", "").replace(CHECK, "").replace("Сверено с первоисточником", "")
    rest = rest.strip(" \t·|—-")
    if "Сверено" in plain:
        return True
    if _LINK_RE.search(line) or _HASHTAG_RE.search(plain):
        return len(rest) <= 3  # подпись ссылки («Первоисточник», «Источник») — внутри <a>, снаружи пусто
    return False


def apply(post_html: str, *, source_url: str, rubric: Rubric, template: str = "") -> str:
    """Срезает подвал модели (и старый формат) и ставит единый подвал кода."""
    lines = post_html.rstrip().split("\n")
    found: list[str] = []
    while lines and (not lines[-1].strip() or _is_footer_line(lines[-1])):
        found += _HASHTAG_RE.findall(_TAG_RE.sub("", _LINK_RE.sub("", lines.pop())))
    allowed = allowed_hashtags(rubric, template)
    tags = [t for t in dict.fromkeys(reversed(found)) if t in allowed] or allowed[:1]
    body = "\n".join(lines).rstrip()
    return body + "\n\n" + footer(source_url, tags)
