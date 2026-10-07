"""Контур «Закреп» (раздел 5): одно сообщение-навигатор, правится раз в день и только при изменении.

Места в топе берёт код из данных источника (адаптеры в leaderboards.py), ИИ ничего не ранжирует.
Если данные не получены, строка остаётся с прошлой датой; если разрешённого способа нет — строки нет.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
from typing import Any

from pydantic import BaseModel

from .board import BoardUnavailable
from .runtime import Runtime
from .telegram import TelegramError

log = logging.getLogger(__name__)

RUBRIC_TAGS = "#инструмент #skill #приём #кейс #срочно #книга #бенчмарк #разбор #словарь #итоги"
DISCLAIMER = ("Это три разных взгляда, а не истина: Arena отражает предпочтения людей, "
              "индекс Artificial Analysis считается по собственным тестам.")


class Snapshot(BaseModel):
    """Топ-3 одного рейтинга на дату источника."""

    key: str
    label: str
    date: str  # дата данных по источнику (YYYY-MM-DD), не дата нашего запроса
    top: list[str]
    data_url: str


def load_snapshots(rt: Runtime) -> dict[str, Snapshot]:
    raw = rt.state.get("pin:snapshots")
    return {k: Snapshot(**v) for k, v in json.loads(raw).items()} if raw else {}


def refresh_snapshots(rt: Runtime, adapters: list[Any]) -> tuple[dict[str, Snapshot], list[str]]:
    """Свежие данные там, где получилось; иначе прошлый снимок (с прошлой датой) и запись в лог."""
    snaps = load_snapshots(rt)
    problems = []
    for a in adapters:
        try:
            s = a.fetch()
            problem = None if s is None else ("в данных меньше трёх моделей" if len(s.top) < 3 else None)
            if s is None:
                problem = "нет данных"
        except Exception as e:  # источник рейтинга недоступен — не повод ломать закреп
            s, problem = None, f"{type(e).__name__}: {str(e)[:120]}"
        if problem:
            problems.append(f"{a.key}: {problem}")
            log.warning("рейтинг %s: %s — в закрепе остаётся прошлая дата", a.key, problem)
            continue
        snaps[a.key] = s
    return snaps, problems


def render(rt: Runtime, snaps: dict[str, Snapshot], adapters: list[Any]) -> str:
    lines = ["📌 <b>Vibe Stack — навигатор</b>"]
    shown = [snaps[a.key] for a in adapters if a.key in snaps]
    if shown:
        dates = sorted({s.date for s in shown})
        when = dates[0] if len(dates) == 1 else f"{dates[0]} — {dates[-1]}"
        lines += ["", f"🏆 <b>Топ моделей</b> (данные на {html.escape(when)})"]
        for s in shown:
            top = "  ".join(f"{i}. {html.escape(m)}" for i, m in enumerate(s.top[:3], 1))
            suffix = f" (на {s.date})" if len(dates) > 1 else ""
            lines.append(f'<a href="{html.escape(s.data_url, quote=True)}">{html.escape(s.label)}</a>{suffix}: {top}')
        lines.append(f"<i>{DISCLAIMER}</i>")
    terms = [r["term"] for r in rt.state.glossary_entries()[:5]]
    page = rt.state.get("glossary:page_url")
    if terms:
        tail = f' · <a href="{html.escape(page, quote=True)}">все термины</a>' if page else ""
        lines += ["", f"📖 <b>Словарь:</b> {html.escape(', '.join(terms))}{tail}"]
    lines += ["", f"🧭 <b>Рубрики:</b> {RUBRIC_TAGS}"]
    return "\n".join(lines)


def run_pin(rt: Runtime, adapters: list[Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"contour": "pin", "mode": rt.mode}
    try:
        settings = rt.settings()
    except BoardUnavailable as e:
        rt.notifier.notify(f"закреп не обновлён: {e}")
        summary["status"] = "board_unavailable"
        return summary
    if settings.pause:
        summary["status"] = "paused"
        return summary

    snaps, problems = refresh_snapshots(rt, adapters)
    summary["problems"] = problems
    text = render(rt, snaps, adapters)
    digest = hashlib.sha256(text.encode()).hexdigest()
    mid = rt.state.get("pin:message_id")
    if mid and rt.state.get("pin:digest") == digest:
        summary["status"] = "unchanged"  # топ и словарь не изменились — не трогаем сообщение
        return summary
    try:
        if mid:
            try:
                rt.tg.edit_message_text(rt.channel_id, int(mid), text)
                summary["status"] = "edited"
            except TelegramError as e:
                err = str(e).lower()
                if "not modified" in err:
                    summary["status"] = "unchanged"
                elif "not found" in err:
                    mid = None  # закреп удалили вручную — создаём заново
                else:
                    raise
        if not mid:
            new_id = rt.tg.send_message(rt.channel_id, text, preview=False)
            rt.tg.pin_chat_message(rt.channel_id, new_id)
            rt.state.put("pin:message_id", str(new_id))
            summary["status"] = "created_and_pinned"
    except TelegramError as e:
        rt.notifier.notify(f"закреп не обновлён: {e}")
        summary["status"] = "telegram_error"
        return summary
    rt.state.put("pin:digest", digest)
    old = load_snapshots(rt)
    rt.state.put("pin:snapshots", json.dumps({k: v.model_dump() for k, v in snaps.items()}, ensure_ascii=False))
    _history(rt, snaps, old)
    rt.write_out("pin.html", text)
    return summary


def _history(rt: Runtime, snaps: dict[str, Snapshot], old: dict[str, Snapshot]) -> None:
    """Таблица «Leaderboard history» в Notion: строка на каждое изменение топа."""
    add = getattr(rt.board, "add_leaderboard_row", None)
    if add is None:
        return
    for k, s in snaps.items():
        if old.get(k) != s:
            try:
                add(s)
            except BoardUnavailable as e:
                log.warning("история рейтинга не записана: %s", e)
