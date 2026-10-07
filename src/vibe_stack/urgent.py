"""Срочный контур: белый список → условия срочности → проверка B (urgent) → короткий пост → сразу в канал.

Срочное определяется не баллами: (1) источник в белом списке и ссылка на его официальный домен,
(2) событие из перечня urgent.events (решает промпт U, перечень проверяет код).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from . import prompts
from .board import BoardUnavailable
from .config import Rubric
from .llm import BudgetExceeded, LLMError
from .models import Candidate, PostRecord, Status, TriageResult
from .publish import board_sent_today, publish_post
from .runtime import Runtime
from .sources import collect_all
from .steps import doc_guards, verify, with_page_title, write
from .timeutil import parse_dt
from .urls import same_site

log = logging.getLogger(__name__)
CONTOUR = "urgent"


def _ukeys(c: Candidate) -> list[str]:
    """Отдельное пространство ключей: «срочный контур уже смотрел» не мешает обычному сбору."""
    return [f"u|{k}" for k in c.keys]


def is_official(c: Candidate) -> bool:
    return c.whitelist and any(same_site(c.domain, d) for d in c.official_domains)


def urgent_date(c: Candidate) -> datetime | None:
    """Дата события: публикация. Дата правки записи фида старую новость свежей не делает
    (исключение — sitemap: там есть только lastmod, поэтому дату дополнительно сверяем по странице)."""
    if c.published_at:
        return c.published_at
    return c.updated_at if c.source_type == "sitemap" else None


def run_urgent(rt: Runtime) -> dict[str, Any]:
    summary: dict[str, Any] = {"contour": CONTOUR, "mode": rt.mode, "checked": 0, "published": 0,
                               "queued_regular": 0, "pending_approval": 0, "not_urgent": 0}
    try:
        settings = rt.settings()
    except BoardUnavailable as e:
        rt.notifier.notify(f"срочный контур пропущен: {e}")
        summary["status"] = "board_unavailable"
        return summary
    if settings.pause:
        summary["status"] = "paused"
        return summary
    limit = settings.urgent_per_day if settings.urgent_per_day is not None else rt.cfg.limits.urgent_per_day
    rubrics = rt.rubrics()
    rubric = rubrics["urgent"]
    if not rubric.enabled:
        summary["status"] = "rubric_disabled"
        return summary
    try:
        # одобренные человеком срочные (режим approve) выходят первыми
        summary["published"] += _publish_approved(rt, limit)
    except BoardUnavailable as e:
        rt.notifier.notify(f"срочный контур остановлен: {e}")
        summary["status"] = "board_unavailable"
        return summary

    now = rt.now()
    ttl = rt.cfg.dedup.seen_ttl_days
    window = timedelta(hours=rt.cfg.urgent.max_item_age_hours)
    raw, errors = collect_all(rt.sources(CONTOUR))
    summary["source_errors"] = errors
    raw.sort(key=lambda c: d.timestamp() if (d := urgent_date(c)) else 0, reverse=True)
    for c in raw:
        if rt.state.seen_outcome(_ukeys(c), now, ttl):
            continue
        if rt.state.seen_outcome(c.keys, now, ttl) in ("published", "queued"):
            continue
        when = urgent_date(c)
        if when is None or now - when > window:
            rt.state.mark_seen(_ukeys(c), c.id, "too_old", now)
            continue
        summary["checked"] += 1
        if not is_official(c):
            rt.decision(CONTOUR, c, "whitelist", "not_urgent", ["not_official_source"])
            rt.state.mark_seen(_ukeys(c), c.id, "not_official", now)
            summary["not_urgent"] += 1
            continue
        try:
            result = _process(rt, c, rubric, limit)
        except BudgetExceeded as e:
            rt.notifier.notify(f"срочный контур остановлен: {e}")
            summary["stopped"] = str(e)
            break
        except LLMError as e:
            rt.decision(CONTOUR, c, "error", "error", ["LLMError"], detail={"error": str(e)[:500]})
            rt.state.mark_seen(_ukeys(c), c.id, "llm_error", now)
            continue
        except BoardUnavailable as e:
            # работа модели уже оплачена, но без доски публиковать нельзя; не повторяем каждые 30 минут
            rt.decision(CONTOUR, c, "error", "error", ["BoardUnavailable"], detail={"error": str(e)[:500]})
            rt.state.mark_seen(_ukeys(c), c.id, "board_error", now)
            rt.notifier.notify(f"срочный контур: Notion не принял пост «{c.title[:80]}»: {e}")
            summary["status"] = "board_unavailable"
            break
        summary[result] = summary.get(result, 0) + 1
    summary.setdefault("status", "ok")
    return summary


def urgent_sent_today(rt: Runtime) -> int:
    on_board = sum(1 for p in board_sent_today(rt) if p.urgent)
    return max(rt.state.count_published(rt.today(), urgent=True), on_board)


def _publish_approved(rt: Runtime, limit: int) -> int:
    """Срочные, одобренные в Notion: в канал, если лимит позволяет, иначе — в очередь обычных."""
    done = 0
    window = timedelta(hours=rt.cfg.urgent.max_item_age_hours)
    for post in [p for p in rt.board.posts_with_status(Status.APPROVED) if p.urgent]:
        if rt.state.is_published_ref(post.ref or ""):
            continue
        stale = post.found_at is not None and rt.now() - post.found_at > window
        if stale or urgent_sent_today(rt) >= limit:
            why = "устарело для срочного" if stale else "лимит срочных исчерпан"
            rt.board.update_post(post.ref, urgent=False, reject_reason=f"{why} — в очереди обычных")
            continue
        if publish_post(rt, post, slot=None, urgent=True, counts_regular=False):
            done += 1
    return done


def _process(rt: Runtime, c: Candidate, rubric: Rubric, limit: int) -> str:
    now = rt.now()
    doc = rt.fetcher.fetch(c.url, purpose="score")
    if not doc.ok:
        rt.decision(CONTOUR, c, "fetch", "fetch_failed", ["source_unavailable"], detail={"error": doc.error})
        return "fetch_failed"
    c = with_page_title(c, doc)
    published = parse_dt(doc.published_meta)
    if published and now - published > timedelta(hours=rt.cfg.urgent.max_item_age_hours):
        # по самой странице событие старое (фид лишь обновил запись)
        rt.decision(CONTOUR, c, "freshness", "not_urgent", [f"published:{doc.published_meta}"])
        rt.state.mark_seen(_ukeys(c), c.id, "too_old", now)
        return "not_urgent"
    if guards := doc_guards(doc, rt.cfg):
        rt.decision(CONTOUR, c, "code_guard", "rejected", guards)
        rt.state.mark_seen(_ukeys(c), c.id, "rejected", now)
        rt.state.mark_seen(c.keys, c.id, "rejected", now)
        return "rejected"

    tri: TriageResult = rt.llm.json("triage", prompts.triage_prompt(c.for_prompt(), doc.text), ctx_id=c.id)
    if tri.hard_stops or tri.event not in rt.cfg.urgent.events:
        reasons = list(tri.hard_stops) or [f"event:{tri.event}"]
        rt.decision(CONTOUR, c, "triage", "not_urgent", reasons)
        rt.state.mark_seen(_ukeys(c), c.id, "not_urgent", now)
        return "not_urgent"

    v = verify(rt, c, tri.claims, mode="urgent")
    if not v.passed:
        rt.decision(CONTOUR, c, "verify", "rejected", v.reasons)
        rt.state.mark_seen(_ukeys(c), c.id, "verify_fail", now)
        return "rejected"
    w = write(rt, c, "urgent", rubric, v.approved, mode="urgent")
    if w.html is None:
        rt.decision(CONTOUR, c, "lint", "rejected", ["lint:" + ";".join(w.errors)])
        rt.state.mark_seen(_ukeys(c), c.id, "lint_fail", now)
        return "rejected"

    verify_json = v.result.model_dump() if v.result else None
    needs_approval = rubric.mode == "approve"
    can_publish = needs_approval or urgent_sent_today(rt) < limit
    post = PostRecord(
        title=c.title, rubric="urgent", status=Status.PENDING if needs_approval else Status.APPROVED,
        mode=rubric.mode, urgent=can_publish, source_url=c.url, source_domain=c.domain, found_at=now, html=w.html,
        verify=verify_json, candidate_id=c.id,
        reject_reason="" if can_publish else "лимит срочных исчерпан — в очереди обычных",
    )
    ref = rt.board.add_post(post)
    post = post.model_copy(update={"ref": ref})
    rt.state.mark_seen(_ukeys(c), c.id, "processed", now)
    if needs_approval or not can_publish:
        rt.state.mark_seen(c.keys, c.id, "queued", now)
        if needs_approval:
            rt.decision(CONTOUR, c, "board", "pending_approval", [], rubric="urgent")
            return "pending_approval"
        rt.decision(CONTOUR, c, "limit", "queued_regular", [f"urgent_limit:{limit}"], rubric="urgent")
        return "queued_regular"
    if publish_post(rt, post, slot=None, urgent=True, counts_regular=False):
        rt.decision(CONTOUR, c, "telegram", "published", [], rubric="urgent")
        return "published"
    rt.decision(CONTOUR, c, "telegram", "error", ["telegram_failed"], rubric="urgent")
    return "error"
