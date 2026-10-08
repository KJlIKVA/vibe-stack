"""Контур «Публикация»: пауза? → план дня (dayplan) → посты, чьё время наступило → Telegram → доска."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import dayplan, footer, images, prompts, sandbox
from .board import BoardUnavailable
from .lint import lint_post
from .models import PostRecord, Status
from .planner import limit_keys, media_group
from .runtime import Runtime
from .telegram import TelegramError
from .urls import dedup_keys, host_of, short_id

log = logging.getLogger(__name__)
CONTOUR = "publish"


MAX_PER_TICK = 2  # если тик опоздал и наступило время двух постов, выходят оба, но не больше


def run_publish(rt: Runtime) -> dict[str, Any]:
    """Тик: досоставить план дня для новых постов и выпустить те, чьё время наступило."""
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
    limit = settings.regular_per_day if settings.regular_per_day is not None else rt.cfg.limits.regular_per_day
    try:
        # счётчики сверяем и с состоянием, и с Notion: если ветка state потерялась, Notion не даст превысить лимит
        on_board = [p for p in board_sent_today(rt) if not p.urgent]
        queue = ready_queue(rt, now)
        sent = max(rt.state.count_published(today, urgent=False), len(on_board))
        if sent >= limit:
            summary["status"] = "daily_limit_reached"
            return summary
        summary["planned"] = len(dayplan.plan(rt, queue, limit=limit, sent_today=sent,
                                              published_times=published_today_times(rt, on_board)))
    except BoardUnavailable as e:
        rt.notifier.notify(f"публикация пропущена: {e}")
        summary["status"] = "board_unavailable"
        return summary

    due = sorted((p for p in queue if p.planned_at and p.planned_at <= now), key=lambda p: p.planned_at or now)
    summary["status"] = "nothing_to_publish"
    if not queue:
        summary["reason"] = "очередь пуста"
    elif not due:
        upcoming = min((p.planned_at for p in queue if p.planned_at), default=None)
        summary["reason"] = (f"следующий пост в {upcoming.astimezone(ZoneInfo(rt.tz)):%d.%m %H:%M}" if upcoming
                             else "в плане нет мест на сегодня")
    for post in due:
        if sent >= limit:
            summary["status"] = "daily_limit_reached"
            break
        if summary["published"] >= MAX_PER_TICK:
            break
        if sandbox.waiting(rt, post, now):
            # ждём песочницу не дольше sandbox.max_wait_minutes, потом пост выйдет и без пометки
            summary["sandbox_waiting"] = summary.get("sandbox_waiting", 0) + 1
            continue
        assert post.planned_at is not None
        ok = publish_post(rt, post, slot=f"{post.planned_at.astimezone(ZoneInfo(rt.tz)):%H:%M}", urgent=False,
                          counts_regular=True)
        if not ok:
            summary["status"] = "error"
            break  # сбой Telegram или Notion — остальное в следующий тик
        sent += 1
        summary["published"] += 1
        summary["status"] = "published"
        summary.setdefault("titles", []).append(post.title)
    return summary


def ready_queue(rt: Runtime, now: datetime) -> list[PostRecord]:
    """Обычные посты «Одобрено», которые ещё можно публиковать (срочные выпускает срочный контур)."""
    queue = rt.board.posts_with_status(Status.APPROVED)
    return _drop_stale_and_published(rt, [p for p in queue if not p.urgent], now)


def published_today_times(rt: Runtime, on_board: list[PostRecord]) -> list[datetime]:
    """Когда сегодня выходили обычные посты (по состоянию и по Notion) — эти слоты в плане заняты."""
    times = [p.published_at for p in on_board if p.published_at]
    times += [h.published_at for h in rt.state.published_since(rt.today())
              if h.local_date == rt.today() and not h.urgent and h.counts_regular]
    return times


def _day_start_utc(rt: Runtime) -> datetime:
    return rt.now_local().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def board_sent_today(rt: Runtime) -> list[PostRecord]:
    """Опубликованное сегодня по данным Notion + «Отправляется» (могло выйти — считаем как вышедшее)."""
    start = _day_start_utc(rt)
    sent = rt.board.published_since(start)
    sending = [p for p in rt.board.posts_with_status(Status.SENDING) if p.published_at and p.published_at >= start]
    return sent + sending


def _drop_stale_and_published(rt: Runtime, queue: list[PostRecord], now: datetime) -> list[PostRecord]:
    fresh = []
    by_key = rt.cfg.schedule.queue_max_age_days_by
    for p in queue:
        days = next((by_key[k] for k in reversed(limit_keys(p.rubric, media_group(p.html))) if k in by_key),
                    rt.cfg.schedule.queue_max_age_days)
        max_age = timedelta(days=days)
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
    if post.rubric == "urgent":  # пост о новой модели длиннее (цена и сравнение)
        max_chars = max(max_chars, rt.cfg.urgent.new_model_max_chars)
    original_html = post.html
    if rubric and post.source_url:
        # подвал собирает код: и у новых постов, и у написанных в старом формате (решение 47)
        template = prompts.footer_template(post.rubric, rubric.overlay)
        post = post.model_copy(update={"html": footer.apply(post.html, source_url=post.source_url, rubric=rubric,
                                                             template=template)})
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
        mid: int | None = rt.tg.send_message(rt.channel_id, text, preview_url=post.source_url or None,
                                             image_url=images.for_post(rt, post))
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
    if group := media_group(text):  # для лимитов планировщика: книги — раз в неделю, видео — раз в день
        rt.state.put(f"group:{ref}", group)
    rt.state.expire_sandbox(post.candidate_id, now)  # пост вышел — запускать его пакет больше незачем
    if mid is None:
        return False
    extra = {"html": text} if text != original_html else {}
    updated = _safe_update(rt, post.ref, status=Status.PUBLISHED, tg_message_id=mid, published_at=now, **extra)
    if post.ref and not updated:
        rt.notifier.notify(f"пост «{post.title[:80]}» вышел (id {mid}), но статус в Notion остался «Отправляется»")
    rt.write_out(f"published/{mid}.html", text)
    if post.rubric == "book_video":  # заголовок для воскресной подборки «Что посмотреть и послушать»
        from .watchlist import remember_headline

        remember_headline(rt, mid, text)
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
