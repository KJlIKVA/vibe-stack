"""Пост «Рейтинг моделей изменился» (решение 58).

Когда в топ-3 любого рейтинга навигатора появилась новая модель или модель сменила место, в канал выходит
короткий текстовый пост без картинки и без хештега: заголовок и по одному предложению на каждый изменившийся
рейтинг, рейтинги отделены пустой строкой.
Текст собирает код из данных Arena, модель не участвует. Изменение одних очков без смены мест — не повод.
Рейтинг без прошлого снимка (только что добавленный) пропускается: сравнивать не с чем.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
from typing import Any

from .footer import CHECK
from .pin import MEDALS, Snapshot
from .runtime import Runtime
from .telegram import TelegramError
from .urls import host_of

log = logging.getLogger(__name__)
RUBRIC = "ratings"
TITLE = "🏆 <b>Рейтинг моделей изменился</b>"


def describe(old: list[str], new: list[str]) -> list[str]:
    """Изменения мест в топ-3: кто вошёл, поднялся, опустился, выбыл. Пусто — места не менялись."""
    parts = []
    for place, model in enumerate(new[:3], 1):
        name = html.escape(model)
        before = old.index(model) + 1 if model in old[:3] else None
        if before == place:
            continue
        if place == 1:
            parts.append(f"{name} — новый лидер {MEDALS[1]}")
        elif before is None:
            parts.append(f"{name} вошла в топ-3 на {MEDALS[place]} место")
        elif place < before:
            parts.append(f"{name} поднялась на {MEDALS[place]} место")
        else:
            parts.append(f"{name} опустилась на {MEDALS[place]} место")
    parts += [f"{html.escape(m)} выбыла из топ-3" for m in old[:3] if m not in new[:3]]
    return parts


def build(pairs: list[tuple[Snapshot, Snapshot]], source_url: str) -> str | None:
    """Текст поста по парам (прошлый снимок, новый) или None, если ни в одном рейтинге места не сменились."""
    lines = []
    for old, new in pairs:
        if parts := describe(old.top, new.top):
            lines.append(f"<b>{html.escape(new.label)}:</b> " + ", ".join(parts) + ".")
    if not lines:
        return None
    link = f'<a href="{html.escape(source_url, quote=True)}">первоисточником</a>'
    return TITLE + "\n\n" + "\n\n".join(lines) + f"\n\nСверено с {link} {CHECK}"


def publish_changes(rt: Runtime, old: dict[str, Snapshot], new: dict[str, Snapshot],
                    adapters: list[Any]) -> dict[str, Any]:
    pairs = [(old[a.key], new[a.key]) for a in adapters if a.key in old and a.key in new]
    changed = [(o, n) for o, n in pairs if describe(o.top, n.top)]
    if not changed:
        return {"status": "no_changes"}
    source_url = changed[0][1].data_url
    text = build(changed, source_url)
    assert text is not None
    # один пост на одно изменение: если закреп упадёт после отправки, следующий запуск пост не повторит
    digest = hashlib.sha256(json.dumps([(n.key, o.top, n.top) for o, n in changed]).encode()).hexdigest()[:16]
    done_key = f"ratings_post:{digest}"
    if rt.state.get(done_key):
        return {"status": "already_posted"}
    rt.write_out("ratings_post.html", text)
    try:
        mid = rt.tg.send_message(rt.channel_id, text, preview=False)
    except TelegramError as e:
        rt.notifier.notify(f"пост об изменении рейтинга не отправлен: {e}")
        return {"status": "telegram_error"}
    rt.state.put(done_key, str(mid))
    rt.state.record_published(ref=f"ratings-{digest}", rubric=RUBRIC, urgent=False, title="Рейтинг моделей изменился",
                              source_url=source_url, domain=host_of(source_url), published_at=rt.now(),
                              day=rt.today(), slot=None, tg_message_id=mid, counts_regular=False)
    log.info("пост об изменении рейтинга: %s", [n.key for _, n in changed])
    return {"status": "published", "changed": [n.key for _, n in changed]}
