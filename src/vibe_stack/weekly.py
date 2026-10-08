"""Контур «Итоги недели»: статистику считает код, текст пишет промпт D."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import timedelta
from typing import Any

from . import prompts
from .board import BoardUnavailable
from .lint import lint_post
from .llm import BudgetExceeded, LLMError
from .runtime import Runtime
from .telegram import TelegramError

log = logging.getLogger(__name__)

REASON_LABELS = {
    "ad": "реклама или партнёрская ссылка",
    "no_primary_source": "нет первоисточника",
    "unsafe_install": "небезопасная установка (curl | bash)",
    "outdated": "устарело",
    "off_topic": "не по теме канала",
    "duplicate_suspected": "похоже на повтор",
    "injection_detected": "попытка prompt injection в тексте",
    "low_score": "мало практической пользы",
    "low_verifiability": "нельзя проверить по источнику",
    "low_usefulness": "не стоит потраченного времени",
    "verify_fail": "утверждения не подтвердились источником",
    "approved_claims": "подтвердилось меньше двух утверждений",
    "verify_source_unavailable": "источник недоступен при проверке",
    "lint": "текст не прошёл автоматическую проверку",
    "blocked_domain": "домен в блок-листе",
    "rubric_disabled": "рубрика пока выключена",
    "no_title": "без заголовка",
}


def reason_label(code: str) -> str:
    base = code.split(":", 1)[0].split("<", 1)[0]
    return REASON_LABELS.get(base, base)


def post_link(rt: Runtime, message_id: int | None) -> str | None:
    return rt.post_link(message_id)


def compute_stats(rt: Runtime) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    end = rt.today()
    start = end - timedelta(days=6)
    # «Слово дня» (отменено решением 56) — не находки из источников: его старые попытки в статистику не идут
    rows = [r for r in rt.state.candidates_between(start, end)
            if r["stage"] not in ("dedup", "deferred") and r["contour"] != "glossary"]
    seen_ids = {r["id"] for r in rows}
    rejected = [r for r in rows if r["decision"] in ("rejected", "not_urgent")]
    by_reason: Counter[str] = Counter()
    for r in rejected:
        for code in json.loads(r["reasons"]) or ["other"]:
            by_reason[reason_label(code)] += 1
    published = [p for p in rt.state.published_since(start) if p.rubric not in ("weekly", "ratings")
                 and p.local_date <= end]
    stats = {
        "period": f"{start.isoformat()} — {end.isoformat()}",
        "просмотрено": len(seen_ids),
        "отклонено": len({r["id"] for r in rejected}),
        "отклонено_по_причинам": dict(by_reason.most_common()),
        "опубликовано": len(published),
        "из_них_срочных": sum(1 for p in published if p.urgent),
    }
    pub_list = [{"заголовок": p.title, "ссылка": post_link(rt, p.tg_message_id)} for p in published]
    examples: list[dict[str, Any]] = []
    used: set[str] = set()
    for r in rejected:  # разнообразие причин: по одному примеру на причину
        if r["stage"] not in ("gate", "verify", "code_guard", "prefilter", "triage"):
            continue
        label = reason_label((json.loads(r["reasons"]) or ["other"])[0])
        if label in used:
            continue
        used.add(label)
        examples.append({"название": r["title"], "причина": label})
        if len(examples) == 5:
            break
    return stats, pub_list, examples


def run_weekly(rt: Runtime) -> dict[str, Any]:
    summary: dict[str, Any] = {"contour": "weekly", "mode": rt.mode, "published": 0}
    try:
        settings = rt.settings()
    except BoardUnavailable as e:
        rt.notifier.notify(f"итоги недели пропущены: {e}")
        summary["status"] = "board_unavailable"
        return summary
    if settings.pause:
        summary["status"] = "paused"
        return summary
    rubric = rt.rubrics()["weekly"]
    if not rubric.enabled:
        summary["status"] = "rubric_disabled"
        return summary
    year, week, _ = rt.today().isocalendar()
    done_key = f"weekly:{year}-W{week:02d}"
    if rt.already_done(done_key):
        summary["status"] = "already_published_this_week"
        return summary
    stats, pub_list, examples = compute_stats(rt)
    summary["stats"] = stats
    try:
        text = rt.llm.text("weekly", prompts.weekly_prompt(stats, pub_list, examples), ctx_id="weekly")
    except (BudgetExceeded, LLMError) as e:
        rt.notifier.notify(f"итоги недели не написаны: {e}")
        summary["status"] = "llm_error"
        return summary
    allowed = json.dumps([stats, pub_list, examples], ensure_ascii=False)
    errors = lint_post(text, rubric="weekly", max_chars=rubric.max_chars, source_url="", weekly=True,
                       allowed_text=allowed, template_text=prompts.load("weekly_D"))
    rt.write_out("weekly.html", text)
    if errors:
        rt.notifier.notify(f"итоги недели не прошли проверку: {';'.join(errors)}")
        summary["status"] = "lint_error"
        summary["errors"] = errors
        return summary
    try:
        mid = rt.tg.send_message(rt.channel_id, text, preview=False)
    except TelegramError as e:
        rt.notifier.notify(f"Telegram не принял итоги недели: {e}")
        summary["status"] = "telegram_error"
        return summary
    rt.mark_done(done_key)
    now = rt.now()
    rt.state.record_published(ref=f"weekly-{rt.today().isoformat()}", rubric="weekly", urgent=False,
                              title="Итоги недели", source_url="", domain="", published_at=now, day=rt.today(),
                              slot=None, tg_message_id=mid, counts_regular=False)
    summary["published"] = 1
    summary["status"] = "published"
    return summary
