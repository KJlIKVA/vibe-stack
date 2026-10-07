"""Контур «Публикация»: пауза? → активный слот → очередь «Одобрено» → планировщик → Telegram → доска."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from . import sandbox
from .board import BoardUnavailable
from .lint import lint_post
from .models import PostRecord, Status
from .planner import pick_next
from .runtime import Runtime
from .telegram import TelegramError
from .urls import dedup_keys, host_of, short_id

log = logging.getLogger(__name__)
CONTOUR = "publish"


def active_slot(now_local: datetime, slots: list[str], window_min: int) -> str | None:
    """Последний наступивший слот, если с его времени прошло меньше window_min минут."""
    current = None
    for s in sorted(slots):
        hh, mm = (int(x) for x in s.split(":"))
        start = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if start <= now_local < start + timedelta(minutes=window_min):
            current = s
    return current


def run_publish(rt: Runtime) -> dict[str, Any]:
    summary: dict[str, Any] = {"contour": CONTOUR, "mode": rt.mode, "published": 0}
    try:
        settings = rt.settings()
    except BoardUnavailable as e:
        # аварийный выключатель не прочитать — значит, не публикуем
        rt.notifier.notify(f"публикация пропущена: {e}")
        summary["status"] = "board_unavailable"
        return summary
    if settings.pause:
        log.info("Пауза включена — публикация пропущена")
        summary["status"] = "paused"
        return summary

    now = rt.now()
    today = rt.today()
    slots = settings.publish_slots or rt.cfg.schedule.publish_slots
    slot = active_slot(rt.now_local(), slots, rt.cfg.schedule.slot_window_minutes)
    if slot is None:
        summary["status"] = "no_active_slot"
        return summary
    limit = settings.regular_per_day if settings.regular_per_day is not None else rt.cfg.limits.regular_per_day
    try:
        # счётчики сверяем и с состоянием, и с Notion: если ветка state потерялась, Notion не даст превысить лимит
        on_board = board_sent_today(rt)
        queue = rt.board.posts_with_status(Status.APPROVED)
    except BoardUnavailable as e:
        rt.notifier.notify(f"публикация пропущена: {e}")
        summary["status"] = "board_unavailable"
        return summary
    regular_on_board = [p for p in on_board if not p.urgent]
    if rt.state.slot_filled(today, slot) or any(_in_slot(rt, p, slot) for p in regular_on_board):
        summary["status"] = f"slot_{slot}_done"
        return summary
    if max(rt.state.count_published(today, urgent=False), len(regular_on_board)) >= limit:
        summary["status"] = "daily_limit_reached"
        return summary

    # срочные (Urgent) публикует срочный контур, здесь только обычная очередь
    queue = _drop_stale_and_published(rt, [p for p in queue if not p.urgent], now)
    if waiting := [p for p in queue if sandbox.waiting(rt, p, now)]:
        # ждём песочницу не дольше sandbox.max_wait_minutes, потом пост выйдет и без пометки
        summary["sandbox_waiting"] = len(waiting)
        queue = [p for p in queue if p not in waiting]
    rubrics = rt.rubrics()
    history = rt.state.published_since(today - timedelta(days=max(rt.cfg.planner.topic_repeat_days, 7) + 1))
    pick = pick_next(
        queue, history, today=today, now=now, cfg=rt.cfg.planner,
        rubric_enabled={k: r.enabled for k, r in rubrics.items()},
    )
    if pick.post is None:
        summary["status"] = "nothing_to_publish"
        summary["reason"] = pick.reason
        log.info("слот %s: публиковать нечего — %s", slot, pick.reason)
        return summary
    post = pick.post
    summary["slot"] = slot
    ok = publish_post(rt, post, slot=slot, urgent=False, counts_regular=True)
    summary["published"] = int(ok)
    summary["status"] = "published" if ok else "error"
    summary["title"] = post.title
    return summary


def _day_start_utc(rt: Runtime) -> datetime:
    return rt.now_local().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def board_sent_today(rt: Runtime) -> list[PostRecord]:
    """Опубликованное сегодня по данным Notion + «Отправляется» (могло выйти — считаем как вышедшее)."""
    start = _day_start_utc(rt)
    sent = rt.board.published_since(start)
    sending = [p for p in rt.board.posts_with_status(Status.SENDING) if p.published_at and p.published_at >= start]
    return sent + sending


def _in_slot(rt: Runtime, p: PostRecord, slot: str) -> bool:
    if not p.published_at:
        return False
    hh, mm = (int(x) for x in slot.split(":"))
    start = rt.now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)
    at = p.published_at.astimezone(start.tzinfo)
    return start <= at < start + timedelta(minutes=rt.cfg.schedule.slot_window_minutes)


def _drop_stale_and_published(rt: Runtime, queue: list[PostRecord], now: datetime) -> list[PostRecord]:
    fresh = []
    max_age = timedelta(days=rt.cfg.schedule.queue_max_age_days)
    for p in queue:
        if p.ref and rt.state.is_published_ref(p.ref):
            # отправлено, но доска не обновилась (например, Notion упал после отправки) — чиним статус
            _safe_update(rt, p.ref, status=Status.PUBLISHED)
            continue
        if p.found_at and now - p.found_at > max_age and p.rubric != "glossary":  # определения не устаревают
            _safe_update(rt, p.ref, status=Status.REJECTED, reject_reason="stale: устарел в очереди")
            continue
        fresh.append(p)
    return fresh


def _safe_update(rt: Runtime, ref: str | None, **fields: Any) -> bool:
    if not ref:
        return False
    try:
        rt.board.update_post(ref, **fields)
        return True
    except Exception as e:
        log.error("доска: не обновить %s: %s", ref, e)
        return False


def publish_post(rt: Runtime, post: PostRecord, *, slot: str | None, urgent: bool, counts_regular: bool) -> bool:
    """Структурная проверка → маркер «Отправляется» → отправка → отметки в состоянии и на доске."""
    rubric = rt.cfg.rubrics.get(post.rubric)
    max_chars = rubric.max_chars if rubric else 900
    # числа здесь не сверяем: текст мог поправить человек при одобрении
    errors = [e for e in lint_post(post.html, rubric=post.rubric, max_chars=max_chars, source_url=post.source_url)
              if not e.startswith("unverified_numbers")]
    if errors:
        _safe_update(rt, post.ref, status=Status.ERROR, reject_reason="lint перед отправкой: " + ";".join(errors))
        rt.notifier.notify(f"пост «{post.title[:80]}» не отправлен: {';'.join(errors)}")
        return False
    now = rt.now()
    if post.ref and not _safe_update(rt, post.ref, status=Status.SENDING, published_at=now):
        # без маркера на доске не отправляем: иначе при сбое после отправки пост мог бы выйти дважды
        rt.notifier.notify(f"пост «{post.title[:80]}» не отправлен: Notion не принял статус «Отправляется»")
        return False
    ref = post.ref or f"tg-{short_id(post.source_url, post.title)}"
    text = sandbox.with_mark(rt, post)  # «🧪 Запущено…» ставит код и только после успешного запуска
    try:
        mid: int | None = rt.tg.send_message(rt.channel_id, text, preview_url=post.source_url or None)
    except TelegramError as e:
        if not e.uncertain:
            _safe_update(rt, post.ref, status=Status.ERROR, reject_reason=f"Telegram: {e}"[:500], published_at=None)
            rt.notifier.notify(f"Telegram не принял пост «{post.title[:80]}»: {e}")
            return False
        # неизвестно, вышел ли пост: считаем вышедшим (лимиты, дедуп), статус «Ошибка», проверка — за человеком
        _safe_update(rt, post.ref, status=Status.ERROR,
                     reject_reason=f"неизвестно, вышел ли пост ({e}) — проверьте канал"[:500])
        rt.notifier.notify(f"неизвестно, вышел ли пост «{post.title[:80]}» ({e}). Проверьте канал; "
                           "повторно автоматически не отправляю")
        mid = None
    _record(rt, post, ref, mid, now, slot=slot, urgent=urgent, counts_regular=counts_regular)
    if mid is None:
        return False
    extra = {"html": text} if text != post.html else {}
    updated = _safe_update(rt, post.ref, status=Status.PUBLISHED, tg_message_id=mid, published_at=now, **extra)
    if post.ref and not updated:
        rt.notifier.notify(f"пост «{post.title[:80]}» вышел (id {mid}), но статус в Notion остался «Отправляется»")
    rt.write_out(f"published/{mid}.html", text)
    log.info("опубликовано: %s (message_id=%s)", post.title, mid)
    if post.rubric == "glossary":
        from .glossary import on_published

        on_published(rt, post, mid, rt.post_link(mid))
    return True


def _record(rt: Runtime, post: PostRecord, ref: str, mid: int | None, now: datetime, *, slot: str | None,
            urgent: bool, counts_regular: bool) -> None:
    rt.state.record_published(
        ref=ref, rubric=post.rubric, urgent=urgent, title=post.title, source_url=post.source_url,
        domain=post.source_domain or host_of(post.source_url), published_at=now, day=rt.today(), slot=slot,
        tg_message_id=mid, counts_regular=counts_regular,
    )
    if post.source_url:
        rt.state.mark_seen(dedup_keys(post.source_url, post.title), post.candidate_id or ref, "published", now)
