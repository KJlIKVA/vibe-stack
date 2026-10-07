"""Промпты A–D и надстройки C — дословно из docs/vibe-stack-package-v2.md (раздел 9).

Плейсхолдеры спецификации (`{{…}}`, `[общий блок безопасности]`, `<meta>{…}</meta>`) подставляются здесь,
сам текст промптов не меняется. Тест tests/test_prompts.py сверяет файлы со спецификацией.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources
from typing import Any

SAFETY_PLACEHOLDER = "[общий блок безопасности]"
DATA_TAGS = ("candidate", "source_document", "claims", "article", "meta", "approved_claims", "stats",
             "published", "rejected_examples")
_DATA_TAG_RE = re.compile(r"<(/?)\s*(" + "|".join(DATA_TAGS) + r")\b[^>]*>", re.IGNORECASE)
_META_RE = re.compile(r"<meta>\{.*?\}</meta>")
_LEFTOVER_RE = re.compile(r"\{\{[^}]*\}\}")

OVERLAY_FILES = {
    "standard": "write_C_standard",
    "urgent": "write_C_urgent",
    "book_video": "write_C_book_video",
    "benchmark": "write_C_benchmark",
    "analysis": "write_C_analysis",
    "glossary": "write_C_glossary",
}


@cache
def load(name: str) -> str:
    return resources.files(__package__).joinpath(f"{name}.txt").read_text(encoding="utf-8")


def safety_preamble() -> str:
    """В шаблонах C и D плейсхолдера нет, но по разделу 9 блок безопасности входит в каждый промпт."""
    return "<безопасность>\n" + load("safety").rstrip("\n") + "\n</безопасность>\n\n"


def neutralize(data: str) -> str:
    """Данные из интернета не могут закрыть наш тег и выйти из «песочницы» <source_document> и т. п."""
    return _DATA_TAG_RE.sub(lambda m: f"‹{m.group(1)}{m.group(2)}›", data)


def render(template: str, values: dict[str, str], meta: dict[str, Any] | None = None) -> str:
    out = template.replace(SAFETY_PLACEHOLDER, load("safety").rstrip("\n"))
    if meta is not None:
        meta_json = json.dumps(meta, ensure_ascii=False)
        out = _META_RE.sub(lambda _: f"<meta>{neutralize(meta_json)}</meta>", out)
    for key, value in values.items():
        placeholder = "{{" + key + "}}"
        if placeholder not in out:
            raise KeyError(f"в шаблоне нет {placeholder}")
        out = out.replace(placeholder, neutralize(value))
    if left := _LEFTOVER_RE.findall(out):
        raise KeyError(f"не подставлены плейсхолдеры: {left}")
    return out


def score_prompt(candidate: dict[str, Any], source_document: str) -> str:
    return render(load("score_A"), {
        "candidate_json": json.dumps(candidate, ensure_ascii=False),
        "первоисточник, обрезанный": source_document,
    })


def triage_prompt(candidate: dict[str, Any], source_document: str) -> str:
    return render(load("triage_U"), {
        "candidate_json": json.dumps(candidate, ensure_ascii=False),
        "первоисточник, обрезанный": source_document,
    })


def glossary_prompt(candidate: dict[str, Any], source_document: str) -> str:
    return render(load("glossary_G"), {
        "candidate_json": json.dumps(candidate, ensure_ascii=False),
        "первоисточник, обрезанный": source_document,
    })


def verify_prompt(claims: list[str], source_document: str, meta: dict[str, Any]) -> str:
    return render(load("verify_B"), {
        "утверждения": json.dumps(claims, ensure_ascii=False),
        "свежезагруженный первоисточник": source_document,
    }, meta=meta)


def write_prompt(approved_claims: list[str], meta: dict[str, Any], overlay: str, emoji: str, hashtag: str,
                 extra: dict[str, str] | None = None) -> str:
    base = render(load("write_C_base"), {
        "утверждения, прошедшие проверку": json.dumps(approved_claims, ensure_ascii=False),
    }, meta=meta)
    ov = load(OVERLAY_FILES[overlay])
    values = dict(extra or {})
    if "{{эмодзи рубрики}}" in ov:
        values["эмодзи рубрики"] = emoji
    ov = ov.replace("#рубрика", hashtag)
    return safety_preamble() + base.rstrip("\n") + "\n\n" + render(ov, values)


def weekly_prompt(stats: dict[str, Any], published: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> str:
    return safety_preamble() + render(load("weekly_D"), {
        "JSON: просмотрено, отклонено по причинам, опубликовано, срочных, рассчитано кодом":
            json.dumps(stats, ensure_ascii=False),
        "опубликованное за неделю: заголовок, ссылка на пост": json.dumps(published, ensure_ascii=False),
        "3–5 показательных отклонённых: название, причина": json.dumps(rejected, ensure_ascii=False),
    })
