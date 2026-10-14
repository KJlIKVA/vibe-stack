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


_ANALYSIS_LEAD = re.compile(r"^\s*<b>\s*О ч[её]м статья\s*:?\s*</b>\s*[—–:-]?\s*", re.IGNORECASE)
_ANALYSIS_HEAD = re.compile(r"^\s*<b>\s*(Что здесь любопытно или спорно|Что с этим делать читателю)\s*:?\s*</b>"
                            r"\s*[—–:-]?\s*(.*?)\s*$", re.IGNORECASE)
# строка, которая начинает новый пункт, а не продолжает абзац: подзаголовок, ссылка, код, список, таймкод, эмодзи
_OWN_LINE = re.compile(r"^(<b>|<i>|<code>|<a\s|<blockquote|[•*·–—-]\s|\d{1,2}[.):]|[^\w«\"'(<])")
_PLAIN_LABEL = re.compile(r"^[^.!?:<]{1,40}:\s")  # «Кому подойдёт: …»
_SENTENCE_END = re.compile(r"[.!?…»\")]$")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁ«\"0-9<])")  # «т. е.» не режем: дальше строчная
MAX_SENTENCES = 3


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _is_prose(block: str) -> bool:
    """Абзац обычного текста в одну строку: не подзаголовок, не список, не ссылка, не «Кому подойдёт: …»."""
    return "\n" not in block and bool(block.strip()) and not _OWN_LINE.match(block) \
        and not _PLAIN_LABEL.match(block)


def _sentences(block: str) -> list[str] | None:
    parts = _SENTENCE_BREAK.split(block)
    return parts if all(_balanced(p) for p in parts) else None  # тег через границу предложений — не режем


def _even(items: list[str]) -> list[str]:
    """Предложения — в абзацы не длиннее MAX_SENTENCES, поровну: 7 → 3 + 2 + 2."""
    k = -(-len(items) // MAX_SENTENCES)
    base, extra = divmod(len(items), k)
    out, i = [], 0
    for n in range(k):
        size = base + (1 if n < extra else 0)
        out.append(" ".join(items[i:i + size]))
        i += size
    return out


def _regroup(run: list[str]) -> list[str]:
    """Подряд идущие абзацы текста. Если большинство из них — одно предложение (модель ставила пустую строку после
    каждого предложения), собираем их в абзацы по 2–3 предложения; длинный абзац делим поровну."""
    split = [_sentences(b) for b in run]
    if len(run) >= 2 and all(split) and 2 * sum(len(p) == 1 for p in split if p) >= len(run):
        return _even([s for p in split if p for s in p])
    return [b if not p or len(p) <= MAX_SENTENCES else "\n\n".join(_even(p)) for b, p in zip(run, split, strict=True)]


def paragraphs(body: str) -> str:
    """Решение 66: текст абзацами. Модель ставила каждое предложение на свою строку (или в свой абзац) — пост читался
    как стена строк. Строки одной мысли склеиваются подряд, абзацы из одного предложения собираются по 2–3,
    абзац длиннее трёх предложений делится, подписи
    разбора: без «О чём статья», а «Что здесь любопытно или спорно» и «Что с этим делать читателю» — отдельной
    строкой, после неё пустая строка. Длинные тире — обычным дефисом. Применяется и к постам в очереди: подвал
    пересобирается при отправке."""
    lines: list[str] = []
    for raw in body.split("\n"):
        line = raw.rstrip()
        if m := _ANALYSIS_LEAD.match(line):
            line = _cap(line[m.end():])
        elif m := _ANALYSIS_HEAD.match(line):
            lines += ["", f"<b>{_cap(m.group(1))}:</b>", ""]
            line = _cap(m.group(2))
            if not line:
                continue
        prev = lines[-1] if lines else ""
        plain_prev = html.unescape(_TAG_RE.sub("", prev)).strip()
        if (len(lines) >= 2 and line and prev and _SENTENCE_END.search(plain_prev)
                and not _OWN_LINE.match(line.lstrip()) and not _PLAIN_LABEL.match(line.lstrip())):
            lines[-1] = f"{prev} {line.strip()}"  # продолжение той же мысли — в ту же строку
        else:
            lines.append(line)
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", "\n".join(lines).strip("\n")) if b.strip()]
    out: list[str] = []
    run: list[str] = []
    for block in [*blocks, ""]:  # пустой блок в конце закрывает последнюю серию
        if _is_prose(block):
            run.append(block)
            continue
        out += _regroup(run) if run else []
        run = []
        if block:
            out.append(block)
    # длинное и среднее тире — обычным дефисом (ваше решение): «API — способ…» → «API - способ…», «1–3» → «1-3»
    return "\n\n".join(out).replace("—", "-").replace("–", "-")


def apply(post_html: str, *, source_url: str, rubric: Rubric, template: str = "") -> str:
    """Срезает подвал модели (и старый формат) и ставит единый подвал кода."""
    lines = post_html.rstrip().split("\n")
    found: list[str] = []
    while lines and (not lines[-1].strip() or _is_footer_line(lines[-1])):
        found += _HASHTAG_RE.findall(_TAG_RE.sub("", _LINK_RE.sub("", lines.pop())))
    lines = [ln for ln in lines if not _is_check_line(ln)]
    allowed = allowed_hashtags(rubric, template)
    tags = [t for t in dict.fromkeys(reversed(found)) if t in allowed] or allowed[:1]
    body = paragraphs(tidy("\n".join(lines))).rstrip()
    return body + "\n\n" + footer(source_url, tags)
