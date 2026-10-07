"""Рубрика «📖 Слово дня» (раздел 7).

Термин публикуется, только если его определяет источник из конфига: промпт G проверяет, что определение
действительно есть в источнике, затем обычная проверка B и текст C по надстройке glossary. После публикации
термин попадает в словарь (SQLite, таблица Glossary в Notion, публичная страница).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from . import prompts
from .board import BoardUnavailable, GlossaryEntry
from .config import GlossaryTerm
from .llm import BudgetExceeded, LLMError
from .models import Candidate, GlossaryResult, PostRecord, Status
from .runtime import Runtime
from .steps import doc_guards, verify, write

log = logging.getLogger(__name__)
CONTOUR = "glossary"
TITLE_PREFIX = "Слово дня: "


def term_candidate(t: GlossaryTerm) -> Candidate:
    return Candidate(source="glossary", source_type="glossary", url=t.source, title=f"{TITLE_PREFIX}{t.term}",
                     extra={"term": t.term})


def _gkeys(t: GlossaryTerm) -> list[str]:
    return [f"g|{t.term.lower()}"]


def pick_terms(rt: Runtime) -> list[GlossaryTerm]:
    """Ещё не опубликованные термины: сначала встретившиеся в постах этой недели, потом стартовый список."""
    done = {r["term"].lower() for r in rt.state.glossary_entries()}
    now = rt.now()
    todo = [t for t in rt.cfg.glossary.terms
            if t.term.lower() not in done and not rt.state.seen_outcome(_gkeys(t), now, 14)]
    try:
        local_now = rt.now_local()
        monday = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local_now.weekday())
        texts = " ".join(f"{p.title} {p.html}" for p in rt.board.published_since(monday)).lower()
    except BoardUnavailable:
        texts = ""
    mentioned = [t for t in todo if any(w.lower() in texts for w in [t.term, *t.aliases])]
    return mentioned + [t for t in todo if t not in mentioned]


def run_glossary(rt: Runtime) -> dict[str, Any]:
    """Готовит один пост «Слово дня» в очередь, если там ещё нет ни одного."""
    summary: dict[str, Any] = {"contour": CONTOUR, "status": "ok", "queued": 0, "rejected": 0}
    rubric = rt.rubrics().get("glossary")
    if rubric is None or not rubric.enabled:
        summary["status"] = "rubric_disabled"
        return summary
    waiting = [p for st in (Status.APPROVED, Status.PENDING) for p in rt.board.posts_with_status(st)
               if p.rubric == "glossary"]
    if waiting:
        summary["status"] = "already_queued"
        return summary
    for t in pick_terms(rt)[:2]:  # не больше двух попыток за запуск
        result = make_term_post(rt, t)
        summary[result] = summary.get(result, 0) + 1
        if result in ("queued", "pending_approval"):
            break
    else:
        if not rt.cfg.glossary.terms:
            summary["status"] = "no_terms"
    return summary


def make_term_post(rt: Runtime, t: GlossaryTerm) -> str:
    rubric = rt.rubrics()["glossary"]
    c = term_candidate(t)
    now = rt.now()
    doc = rt.fetcher.fetch(c.url, purpose="score")
    if not doc.ok:
        rt.decision(CONTOUR, c, "fetch", "rejected", ["no_source"], detail={"error": doc.error})
        rt.state.mark_seen(_gkeys(t), c.id, "rejected", now)
        return "rejected"
    if guards := doc_guards(doc, rt.cfg):  # без проверки дат: определение не устаревает за 30 дней
        rt.decision(CONTOUR, c, "code_guard", "rejected", guards)
        rt.state.mark_seen(_gkeys(t), c.id, "rejected", now)
        return "rejected"
    try:
        g: GlossaryResult = rt.llm.json("glossary", prompts.glossary_prompt(
            {"id": c.id, "term": t.term, "aliases": t.aliases, "source": t.source}, doc.text), ctx_id=c.id)
        if not g.has_definition or g.hard_stops or len(g.claims) < 2:
            reasons = list(g.hard_stops) or ["no_definition_in_source"]
            rt.decision(CONTOUR, c, "glossary", "rejected", reasons)
            rt.state.mark_seen(_gkeys(t), c.id, "rejected", now)
            return "rejected"
        claims = [*g.claims, *(x for x in (g.example, g.not_to_confuse) if x.strip())]
        v = verify(rt, c, claims, mode="standard")
        if not v.passed:
            rt.decision(CONTOUR, c, "verify", "rejected", v.reasons)
            rt.state.mark_seen(_gkeys(t), c.id, "rejected", now)
            return "rejected"
        w = write(rt, c, "glossary", rubric, v.approved, mode="standard", extra={"термин": t.term})
    except (LLMError, BudgetExceeded) as e:
        rt.decision(CONTOUR, c, "error", "error", [type(e).__name__], detail={"error": str(e)[:300]})
        return "error"
    if w.html is None:
        rt.decision(CONTOUR, c, "lint", "rejected", ["lint:" + ";".join(w.errors)])
        rt.state.mark_seen(_gkeys(t), c.id, "rejected", now)
        return "rejected"
    status = Status.APPROVED if rubric.mode == "auto" else Status.PENDING
    rt.board.add_post(PostRecord(
        title=c.title, rubric="glossary", status=status, mode=rubric.mode, source_url=c.url,
        source_domain=c.domain, found_at=now, html=w.html, verify=v.result.model_dump() if v.result else None,
        candidate_id=c.id,
    ))
    rt.state.mark_seen(_gkeys(t), c.id, "queued", now)
    rt.write_out(f"posts/glossary-{c.id}.html", w.html)
    decision = "queued" if status == Status.APPROVED else "pending_approval"
    rt.decision(CONTOUR, c, "board", decision, [], rubric="glossary")
    return decision


def on_published(rt: Runtime, post: PostRecord, message_id: int, post_url: str | None) -> None:
    """После выхода поста: термин — в словарь (SQLite, Notion), страница словаря пересобирается."""
    term = post.title.removeprefix(TITLE_PREFIX).strip()
    checks = (post.verify or {}).get("checks") or []
    definition = " ".join(ch["claim"] for ch in checks if ch.get("status") == "supported")[:1900]
    now = rt.now()
    rt.state.add_glossary(term=term, source_url=post.source_url, definition=definition, published_at=now,
                          post_url=post_url)
    try:
        rt.board.add_glossary(GlossaryEntry(term=term, definition=definition, source_url=post.source_url,
                                            published_at=now, post_url=post_url))
    except BoardUnavailable as e:
        rt.notifier.notify(f"термин «{term}» опубликован, но не записан в Notion: {e}")
    if rt.glossary_page is not None:
        try:
            url = rt.glossary_page.sync(rt.state.glossary_entries())
            log.info("страница словаря обновлена: %s", url)
        except Exception as e:  # страница словаря не должна ломать публикацию
            rt.notifier.notify(f"не удалось обновить страницу словаря: {type(e).__name__}: {str(e)[:200]}")
