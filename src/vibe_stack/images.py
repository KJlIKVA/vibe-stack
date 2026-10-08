"""Картинка поста (решение 47): большое превью над текстом.

- Обычный пост — главная картинка страницы первоисточника (og:image) или карточка репозитория GitHub.
- Пост о новой модели — модель смотрит картинки статьи (в низком разрешении) и выбирает таблицу или график
  бенчмарков, а если их нет — цен. Не нашла — берём главную картинку.

Адрес запоминается в состоянии по кандидату: публикация может быть через несколько часов после сбора.
Картинка — только украшение: если её нет или Telegram не построил превью, пост выходит как обычно.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import prompts
from .llm import BudgetExceeded, LLMError
from .models import Candidate, FetchedDoc, ImagePick, PostRecord

log = logging.getLogger(__name__)


def _key(candidate_id: str) -> str:
    return f"image:{candidate_id}"


def remember(rt: Any, candidate_id: str | None, url: str | None) -> None:
    if rt.cfg.images.enabled and candidate_id and url:
        rt.state.put(_key(candidate_id), url)


def for_post(rt: Any, post: PostRecord) -> str | None:
    if not rt.cfg.images.enabled or not post.candidate_id:
        return None
    return rt.state.get(_key(post.candidate_id))


def image_prompt(candidate: dict[str, Any], count: int) -> str:
    return prompts.render(prompts.load("image_I"), {
        "candidate_json": json.dumps(candidate, ensure_ascii=False),
        "последний номер": str(count - 1),
    })


def pick_chart(rt: Any, c: Candidate, doc: FetchedDoc) -> str | None:
    """Таблица/график бенчмарков или цен из картинок статьи; None — не нашлось или модель недоступна."""
    figures = doc.figures[: rt.cfg.images.max_figures]
    if not rt.cfg.images.enabled or not figures:
        return None
    try:
        pick: ImagePick = rt.llm.json("image", image_prompt(c.for_prompt(), len(figures)), ctx_id=f"{c.id}#image",
                                      images=figures)
    except (LLMError, BudgetExceeded) as e:
        log.info("картинка для %s не выбрана: %s", c.id, type(e).__name__)
        return None
    if pick.kind == "none" or not 0 <= pick.index < len(figures):
        return None
    log.info("картинка для %s: %s №%d", c.id, pick.kind, pick.index)
    return figures[pick.index]


def choose(rt: Any, c: Candidate, doc: FetchedDoc, *, new_model: bool = False) -> str | None:
    if not rt.cfg.images.enabled:
        return None
    return (pick_chart(rt, c, doc) if new_model else None) or doc.image
