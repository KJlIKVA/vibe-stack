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

NAV_TITLE = "📌 Vibe Stack — навигатор"  # так начинается текст навигатора (getChat отдаёт текст без разметки)
DISCLAIMER_FULL = ("Это разные взгляды, а не истина: Arena отражает предпочтения людей, "
                   "индекс Artificial Analysis считается по собственным тестам.")


class Snapshot(BaseModel):
    """Топ-3 одного рейтинга на дату источника."""

    key: str
    label: str
    date: str  # дата данных по источнику (YYYY-MM-DD), не дата нашего запроса
    top: list[str]
    data_url: str
    attribution: str = ""  # подпись источника (HTML), обязательна по лицензии/условиям


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
    """Навигатор: каждый рейтинг — заголовок и места столбиком, между блоками пустая строка (решение 54)."""
    lines = [f"📌 <b>{NAV_TITLE.removeprefix('📌 ')}</b>"]
    shown = [snaps[a.key] for a in adapters if a.key in snaps]
    if shown:
        # «Топ моделей» — ссылка на данные: это указание источника по CC BY 4.0 (п. 3(a)(2) лицензии разрешает
        # ссылку на страницу с автором и лицензией), отдельная подпись снизу не нужна
        url = html.escape(shown[0].data_url, quote=True)
        lines += ["", f'🏆 <b><a href="{url}">Топ моделей</a></b>']
        for s in shown:
            lines += ["", f"<b>{html.escape(s.label)}</b>"]
            lines += [f"{i}. {html.escape(m)}" for i, m in enumerate(s.top[:3], 1)]
        others = [s for s in shown if not s.key.startswith("arena")]
        if others:  # у Artificial Analysis подпись обязательна по их условиям — она остаётся текстом
            lines += ["", f"<i>{DISCLAIMER_FULL}</i>"]
            lines += [f"<i>{a}</i>" for a in dict.fromkeys(s.attribution for s in others if s.attribution)]
    terms = [r["term"] for r in rt.state.glossary_entries()[:5]]
    page = rt.cfg.glossary.telegraph_url or rt.state.get("glossary:page_url")
    if terms:
        tail = f' · <a href="{html.escape(page, quote=True)}">все термины</a>' if page else ""
        lines += ["", f"📖 <b>Словарь:</b> {html.escape(', '.join(terms))}{tail}"]
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
    if problems:
        rt.notifier.notify("закреп: рейтинг не обновлён — " + "; ".join(problems)[:500])
    text = render(rt, snaps, adapters)
    digest = hashlib.sha256(text.encode()).hexdigest()
    mid = rt.state.get("pin:message_id")
    pinned = _current_pin(rt)
    if pinned == {}:
        rt.state.put("pin:pinned", "")  # в канале ничего не закреплено: наш закреп открепили или удалили
    if not mid and pinned and _is_navigator(pinned):
        mid = str(pinned["message_id"])  # состояние потеряно, но навигатор уже висит — правим его, не дублируем
        rt.state.put("pin:message_id", mid)
        rt.state.put("pin:pinned", mid)
    if mid and rt.state.get("pin:digest") == digest and _pinned_id(rt, mid) == mid:
        summary["status"] = "unchanged"  # топ и словарь не изменились — не трогаем сообщение
        return summary
    text_posted = rt.state.get("pin:digest") == digest  # актуальный текст уже в канале
    try:
        if mid and not text_posted:
            try:
                rt.tg.edit_message_text(rt.channel_id, int(mid), text)
                text_posted = True
                summary["status"] = "edited"
            except TelegramError as e:
                if "not modified" in str(e).lower():
                    text_posted = True
                    summary["status"] = "unchanged"
                elif "not found" in str(e).lower():
                    mid = None  # закреп удалили вручную — создаём заново
                else:
                    raise
        if mid and _pinned_id(rt, mid) != mid:
            try:
                rt.tg.pin_chat_message(rt.channel_id, int(mid))
                rt.state.put("pin:pinned", mid)
                summary.setdefault("status", "pinned")
            except TelegramError as e:
                if "not found" not in str(e).lower():
                    raise
                mid = None  # сообщение удалено — создаём заново
        if not mid:
            mid = str(rt.tg.send_message(rt.channel_id, text, preview=False))
            # id сохраняем сразу: если закрепить не выйдет, следующий запуск только повторит закрепление,
            # а не отправит второй навигатор
            rt.state.put("pin:message_id", mid)
            rt.state.put("pin:pinned", "")
            text_posted = True
            rt.tg.pin_chat_message(rt.channel_id, int(mid))
            rt.state.put("pin:pinned", mid)
            summary["status"] = "created_and_pinned"
    except TelegramError as e:
        rt.notifier.notify(f"закреп не обновлён: {e}")
        summary["status"] = "telegram_error"
        if text_posted:
            rt.state.put("pin:digest", digest)  # текст уже в канале — следующий запуск только закрепит
        return summary
    rt.state.put("pin:digest", digest)
    old = load_snapshots(rt)
    rt.state.put("pin:snapshots", json.dumps({k: v.model_dump() for k, v in snaps.items()}, ensure_ascii=False))
    _history(rt, snaps, old)
    rt.write_out("pin.html", text)
    return summary


def _pinned_id(rt: Runtime, mid: str | None) -> str | None:
    """Какое наше сообщение закреплено. Без записи (состояние до этого флага) — считаем, что закреплён mid."""
    v = rt.state.get("pin:pinned")
    return mid if v is None else v


def _current_pin(rt: Runtime) -> dict[str, Any] | None:
    """Закреплённое сейчас сообщение; None — узнать не удалось (тогда полагаемся на состояние)."""
    get = getattr(rt.tg, "pinned_message", None)
    if get is None:
        return None
    try:
        return get(rt.channel_id)
    except TelegramError as e:
        log.warning("не узнать закреплённое сообщение: %s", e)
        return None


def _is_navigator(msg: dict[str, Any]) -> bool:
    return str(msg.get("text", "")).startswith(NAV_TITLE)


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
