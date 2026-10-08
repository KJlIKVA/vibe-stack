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


def _is_check_line(line: str) -> bool:
    """Отметка «✅ Сверено с первоисточником» без другого текста — модель ставит её и посреди поста
    (у «Книги/видео» после неё идёт «Где взять»)."""
    plain = html.unescape(_TAG_RE.sub("", line))
    return "Сверено" in plain and not plain.replace(CHECK, "").replace("Сверено с первоисточником", "") \
        .replace("Сверено с первоисточник", "").strip(" \t·|—-.ом")


_LABEL_RE = re.compile(r"^(\s*<b>[^<]{1,40}:</b>)\s*(.*?)\s*$")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_POINTER_VERB = re.compile(r"(смотр|ищ|найд|загля|читай|уточн|см\.)", re.IGNORECASE)
_DEICTIC = re.compile(r"^(Там|Здесь|В нём|В ней)\b")
_PAIRS = (("<b>", "</b>"), ("<i>", "</i>"), ("<code>", "</code>"), ("<a ", "</a>"))


def _balanced(s: str) -> bool:
    return all(s.count(a) == s.count(b) for a, b in _PAIRS)


def _is_pointer(sentence: str) -> bool:
    """Пустая отсылка к первоисточнику вместо содержания: «подробности смотрите в первоисточнике»."""
    plain = html.unescape(_TAG_RE.sub("", sentence)).lower()
    return "первоисточник" in plain and bool(_POINTER_VERB.search(plain)) and len(plain) <= 160


def tidy(body: str) -> str:
    """Решение 57: «Подводный камень» → «Подводные камни»; пустые отсылки к первоисточнику («подробности
    смотрите в первоисточнике», «ищите в первоисточнике») убираются, а строка, в которой ничего не осталось,
    не пишется вовсе. Применяется и к постам, написанным раньше: подвал пересобирается при отправке."""
    out: list[str] = []
    changed = False
    for line in body.split("\n"):
        line = line.replace("<b>Подводный камень:</b>", "<b>Подводные камни:</b>")
        m = _LABEL_RE.match(line)
        label, content = (m.group(1), m.group(2)) if m else ("", line.strip())
        sentences = [s for s in _SENTENCE_SPLIT.split(content) if s]
        kept: list[str] = []
        dropped_prev = False
        for s in sentences:
            # после выброшенной отсылки и «Там указан…» — про тот же первоисточник
            if _balanced(s) and (_is_pointer(s) or (dropped_prev and _DEICTIC.match(s))):
                dropped_prev = True
                continue
            dropped_prev = False
            kept.append(s)
        if len(kept) == len(sentences):
            out.append(line)
            continue
        changed = True
        if kept:
            out.append(f"{label} {' '.join(kept)}".strip())
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text) if changed else text


def apply(post_html: str, *, source_url: str, rubric: Rubric, template: str = "") -> str:
    """Срезает подвал модели (и старый формат) и ставит единый подвал кода."""
    lines = post_html.rstrip().split("\n")
    found: list[str] = []
    while lines and (not lines[-1].strip() or _is_footer_line(lines[-1])):
        found += _HASHTAG_RE.findall(_TAG_RE.sub("", _LINK_RE.sub("", lines.pop())))
    lines = [ln for ln in lines if not _is_check_line(ln)]
    allowed = allowed_hashtags(rubric, template)
    tags = [t for t in dict.fromkeys(reversed(found)) if t in allowed] or allowed[:1]
    body = tidy("\n".join(lines)).rstrip()
    return body + "\n\n" + footer(source_url, tags)
