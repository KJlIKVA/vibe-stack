"""План дня (решение владельца канала, 2026-10-07): бот сам ставит каждому одобренному посту время.

- Сразу после сбора посты расставляются равномерно по свободным слотам до конца дня. Сетка —
  schedule.publish_slots (сейчас каждые 30 минут 08:00–23:30). Учитываются дневной лимит и правила
  планировщика из раздела 4: домены, темы, недельные квоты. Одна рубрика подряд допускается, только если
  других постов нет.
- Время пишется в Notion («Время публикации»): его можно поменять, а пост отклонить. Админу приходит
  «План на сегодня».
- Публикация (tick каждые 30 минут) выпускает посты, у которых время наступило. Молчание = согласие.
- Посты, одобренные позже (например, «Разбор»), занимают ближайший свободный слот.
- Если время прошло больше чем на max_late_minutes (пауза, сбой GitHub), пост не выходит пачкой
  вместе с другими, а получает новое время.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .board import BoardUnavailable
from .models import PostRecord
from .planner import pick_next
from .runtime import Runtime
from .storage import PublishedRow
from .timeutil import local_date
from .urls import host_of

log = logging.getLogger(__name__)
MAX_LATE_MINUTES = 90


def slot_grid(day: date, slots: list[str], tz: str) -> list[datetime]:
    zone = ZoneInfo(tz)
    out = []
    for s in sorted(slots):
        hh, mm = (int(x) for x in s.split(":"))
        out.append(datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone))
    return out


def _slot_of(t: datetime, grid: list[datetime], window: timedelta) -> datetime | None:
    """Слот, в окно которого попадает момент t."""
    for g in reversed(grid):
        if g <= t < g + window:
            return g
    return None


def _row(p: PostRecord, at: datetime, tz: str) -> PublishedRow:
    """Запланированный пост как «вышедший» — чтобы правила планировщика видели план целиком."""
    return PublishedRow(ref=p.ref or p.title, rubric=p.rubric, urgent=False, title=p.title, source_url=p.source_url,
                        domain=p.source_domain or host_of(p.source_url), published_at=at,
                        local_date=local_date(at, tz), slot=None, tg_message_id=None, counts_regular=True)


def spread(free: list[datetime], k: int) -> list[datetime]:
    """k моментов из free равномерно по дню; один пост — в ближайший слот."""
    if k <= 0:
        return []
    if k >= len(free):
        return free[:k]
    if k == 1:
        return [free[0]]
    return [free[round(i * (len(free) - 1) / (k - 1))] for i in range(k)]


def plan(rt: Runtime, queue: list[PostRecord], *, limit: int, sent_today: int,
         published_times: list[datetime]) -> list[tuple[PostRecord, datetime]]:
    """Ставит время постам очереди, у которых его нет (или оно безнадёжно прошло). Пишет время в Notion.

    queue — обычные посты «Одобрено». published_times — когда сегодня уже выходили посты (их слоты заняты).
    """
    now = rt.now()
    tz = rt.tz
    today = rt.today()
    settings = rt.settings()
    slots = settings.publish_slots or rt.cfg.schedule.publish_slots
    window = timedelta(minutes=rt.cfg.schedule.slot_window_minutes)
    grid = slot_grid(today, slots, tz)

    late = [p for p in queue if p.planned_at and p.planned_at < now - timedelta(minutes=MAX_LATE_MINUTES)]
    planned = [p for p in queue if p.planned_at and p not in late]
    unplanned = [p for p in queue if not p.planned_at or p in late]
    if not unplanned:
        return []

    taken = {_slot_of(t, grid, window) for t in [*published_times, *(p.planned_at for p in planned if p.planned_at)]}
    free = [g for g in grid if g + window > now and g not in taken]  # текущий слот тоже годится, если он свободен
    planned_today = sum(1 for p in planned if p.planned_at and local_date(p.planned_at, tz) == today)
    capacity = max(0, min(limit - sent_today - planned_today, len(free)))

    history = rt.state.published_since(today - timedelta(days=max(rt.cfg.planner.topic_repeat_days, 7) + 1))
    history += [_row(p, p.planned_at, tz) for p in planned if p.planned_at]
    rubrics = rt.rubrics()
    enabled = {k: r.enabled for k, r in rubrics.items()}
    order: list[PostRecord] = []
    remaining = list(unplanned)
    while remaining and len(order) < capacity:
        pick = pick_next(remaining, history, today=today, now=now, cfg=rt.cfg.planner, rubric_enabled=enabled,
                         soft_rubric_repeat=True)
        if pick.post is None:
            log.info("план дня: остальные посты не подходят сегодня — %s", pick.reason)
            break
        order.append(pick.post)
        remaining.remove(pick.post)
        history.append(_row(pick.post, now + timedelta(minutes=len(order)), tz))  # порядок важен, не время
    for p in late:
        if p not in order:  # опоздавший пост без нового места не должен выйти по старому времени
            p.planned_at = None
            if p.ref:
                try:
                    rt.board.update_post(p.ref, planned_at=None)
                except BoardUnavailable as e:
                    log.warning("план дня: не снять старое время у «%s»: %s", p.title[:60], e)
    times = spread(free, len(order))
    out = []
    for p, t in zip(order, times, strict=True):
        try:
            if p.ref:
                rt.board.update_post(p.ref, planned_at=t)
        except BoardUnavailable as e:
            log.warning("план дня: время для «%s» не записано в Notion: %s", p.title[:60], e)
            continue
        p.planned_at = t
        out.append((p, t))
    return out


def plan_text(rt: Runtime, queue: list[PostRecord], *, update: bool = False) -> str:
    """«План на сегодня» для админа: всё, что стоит на сегодня, по времени. update — после дневного сбора."""
    tz = rt.tz
    today = rt.today()
    todays = sorted((p for p in queue if p.planned_at and local_date(p.planned_at, tz) == today),
                    key=lambda p: p.planned_at or datetime.min)
    if not todays:
        return ""
    rubrics = rt.cfg.rubrics
    head = "🗓 Обновлённый план" if update else "🗓 План"
    lines = [f"{head} на {today:%d.%m}: {len(todays)} пост(ов)" + (" — добавлены посты дневного сбора" if update
                                                                   else ""), ""]
    for p in todays:
        assert p.planned_at is not None
        emoji = rubrics[p.rubric].title.split(" ", 1)[0] if p.rubric in rubrics else "•"
        lines.append(f"{p.planned_at.astimezone(ZoneInfo(tz)):%H:%M} {emoji} {p.title[:90]}")
    lines += ["", "Поменять время — в Notion, столбец «Время публикации»; отменить — статус «Отклонено». "
                  "Если ничего не делать, посты выйдут по плану."]
    return "\n".join(lines)


def plan_and_report(rt: Runtime, *, update: bool = False) -> dict[str, Any]:
    """После сбора: план на весь день и сообщение админу. update — дневной сбор: только новые посты в свободные
    слоты, сообщение — если что-то добавилось."""
    from .publish import board_sent_today, published_today_times, ready_queue

    summary: dict[str, Any] = {"planned": 0}
    try:
        settings = rt.settings()
        if settings.pause:
            summary["status"] = "paused"
            return summary
        limit = settings.regular_per_day if settings.regular_per_day is not None else rt.cfg.limits.regular_per_day
        on_board = [p for p in board_sent_today(rt) if not p.urgent]
        queue = ready_queue(rt, rt.now())
        sent = max(rt.state.count_published(rt.today(), urgent=False), len(on_board))
        assigned = plan(rt, queue, limit=limit, sent_today=sent, published_times=published_today_times(rt, on_board))
    except BoardUnavailable as e:
        rt.notifier.notify(f"план дня не составлен: {e}")
        summary["status"] = "board_unavailable"
        return summary
    summary["planned"] = len(assigned)
    if update and not assigned:
        summary["status"] = "ok"
        return summary
    if text := plan_text(rt, queue, update=update):
        rt.notifier.notify(text)
        rt.write_out("plan.txt", text)
    summary["status"] = "ok"
    return summary
