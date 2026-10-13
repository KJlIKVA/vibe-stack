"""Срочный контур: белый список → условия срочности → проверка B (urgent) → короткий пост → сразу в канал.

Срочное определяется не баллами: (1) источник в белом списке и ссылка на его официальный домен,
(2) событие из перечня urgent.events (решает промпт U, перечень проверяет код). Обычного дневного лимита у
срочных нет — срочное на то и срочное (решение 65). Есть только предохранитель от сбоя: больше
limits.urgent_per_day (20) за день срочные не выходят, лишние ждут в очереди обычных, а админу приходит оповещение.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from . import images, prompts
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


_MODEL_NOISE = {"claude", "anthropic", "openai", "google", "deepmind", "model", "the", "new"}
MODEL_MEMORY_DAYS = 30


def model_key(name: str) -> str:
    """Ключ модели для «один пост на модель»: «Claude Haiku 5.5», «claude-haiku-5-5» и «Haiku 5.5» → «haiku 5.5»;
    «GPT-6.1 Sol» и «gpt-6.1-sol» → «gpt 6.1 sol»."""
    out: list[str] = []
    for w in re.findall(r"[a-z]+|\d+(?:\.\d+)*", name.lower()):
        if w in _MODEL_NOISE:
            continue
        if w.isdigit() and out and re.fullmatch(r"\d+(?:\.\d+)*", out[-1]):
            out[-1] += "." + w  # id через дефисы: 5-5 → 5.5
        else:
            out.append(w)
    return " ".join(out)


def model_posted(rt: Runtime, key: str) -> dict[str, Any] | None:
    """Пост о модели за последние MODEL_MEMORY_DAYS дней: {"day": …, "vendor": от самой компании или нет}."""
    raw = rt.state.get(f"model_post:{key}")
    if not raw:
        return None
    seen = json.loads(raw)
    if (rt.today() - datetime.fromisoformat(seen["day"]).date()).days > MODEL_MEMORY_DAYS:
        return None
    return seen


def remember_model(rt: Runtime, key: str, vendor: bool) -> None:
    if (seen := model_posted(rt, key)) and seen["vendor"]:
        return
    rt.state.put(f"model_post:{key}", json.dumps({"day": rt.today().isoformat(), "vendor": vendor}))


def is_vendor(rt: Runtime, c: Candidate) -> bool:
    """Источник самой компании — разработчика модели (model_vendor в config.yaml)."""
    return any(s.model_vendor for s in rt.cfg.sources if s.name == c.source)


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
    rubrics = rt.rubrics()
    rubric = rubrics["urgent"]
    if not rubric.enabled:
        summary["status"] = "rubric_disabled"
        return summary
    try:
        # одобренные человеком срочные (режим approve) выходят первыми
        summary["published"] += _publish_approved(rt)
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
            result = _process(rt, c, rubric)
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


def fuse_blown(rt: Runtime) -> bool:
    """Предохранитель (решение 65): limits.urgent_per_day срочных за день — это уже похоже на сбой, а не на новости.
    Лимит только из config.yaml: «Лимит срочных» в Notion больше не читается."""
    limit = rt.cfg.limits.urgent_per_day
    if urgent_sent_today(rt) < limit:
        return False
    key = f"urgent_fuse:{rt.today().isoformat()}"
    if not rt.state.get(key):
        rt.state.put(key, "1")
        rt.notifier.notify(f"срочный контур: за сегодня вышло {limit} срочных — сработал предохранитель, это похоже "
                           "на сбой. Следующие срочные до конца дня ждут в очереди обычных; проверьте лог срочного "
                           "контура в GitHub Actions.")
    return True


def _publish_approved(rt: Runtime) -> int:
    """Срочные, одобренные в Notion: сразу в канал; устаревшие для срочного или сверх предохранителя — в очередь
    обычных."""
    done = 0
    window = timedelta(hours=rt.cfg.urgent.max_item_age_hours)
    for post in [p for p in rt.board.posts_with_status(Status.APPROVED) if p.urgent]:
        if rt.state.is_published_ref(post.ref or ""):
            continue
        stale = post.found_at is not None and rt.now() - post.found_at > window
        if stale or fuse_blown(rt):
            why = "устарело для срочного" if stale else "сработал предохранитель срочных"
            rt.board.update_post(post.ref, urgent=False, reject_reason=f"{why} — в очереди обычных")
            continue
        if publish_post(rt, post, slot=None, urgent=True, counts_regular=False):
            done += 1
    return done


def _process(rt: Runtime, c: Candidate, rubric: Rubric) -> str:
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
    # одна модель — один срочный пост (решение 65). Пост от самой компании главный: его не остановит пост о той же
    # модели с чужого сайта (GitHub Copilot, облака), а такие посты после него уже не срочные
    new_model = tri.event == "new_model"  # цена и сравнение с другими моделями — отдельным блоком (решение 47)
    vendor = new_model and is_vendor(rt, c)
    model = model_key(tri.model or c.title) if new_model else ""
    if model and (seen := model_posted(rt, model)) and (seen["vendor"] or not vendor):
        rt.decision(CONTOUR, c, "triage", "not_urgent", [f"model_already_posted:{model}"])
        rt.state.mark_seen(_ukeys(c), c.id, "not_urgent", now)
        return "not_urgent"

    v = verify(rt, c, tri.claims, mode="urgent")
    if not v.passed:
        rt.decision(CONTOUR, c, "verify", "rejected", v.reasons)
        rt.state.mark_seen(_ukeys(c), c.id, "verify_fail", now)
        return "rejected"
    w = write(rt, c, "urgent", rubric, v.approved, mode="urgent",
              notes=prompts.load("model_release") if new_model else None,
              max_chars=rt.cfg.urgent.new_model_max_chars if new_model else None)
    if w.html is None:
        rt.decision(CONTOUR, c, "lint", "rejected", ["lint:" + ";".join(w.errors)])
        rt.state.mark_seen(_ukeys(c), c.id, "lint_fail", now)
        return "rejected"

    verify_json = v.result.model_dump() if v.result else None
    needs_approval = rubric.mode == "approve"
    can_publish = needs_approval or not fuse_blown(rt)
    post = PostRecord(
        title=c.title, rubric="urgent", status=Status.PENDING if needs_approval else Status.APPROVED,
        mode=rubric.mode, urgent=can_publish, source_url=c.url, source_domain=c.domain, found_at=now, html=w.html,
        verify=verify_json, candidate_id=c.id,
        reject_reason="" if can_publish else "сработал предохранитель срочных — в очереди обычных",
    )
    ref = rt.board.add_post(post)
    post = post.model_copy(update={"ref": ref})
    rt.state.mark_seen(_ukeys(c), c.id, "processed", now)
    if model:
        remember_model(rt, model, vendor)
    images.remember(rt, c.id, images.choose(rt, c, doc, new_model=new_model))
    if needs_approval or not can_publish:
        rt.state.mark_seen(c.keys, c.id, "queued", now)
        if needs_approval:
            rt.decision(CONTOUR, c, "board", "pending_approval", [], rubric="urgent")
            return "pending_approval"
        rt.decision(CONTOUR, c, "limit", "queued_regular", [f"urgent_fuse:{rt.cfg.limits.urgent_per_day}"],
                    rubric="urgent")
        return "queued_regular"
    if publish_post(rt, post, slot=None, urgent=True, counts_regular=False):
        rt.decision(CONTOUR, c, "telegram", "published", [], rubric="urgent")
        return "published"
    rt.decision(CONTOUR, c, "telegram", "error", ["telegram_failed"], rubric="urgent")
    return "error"
