"""Рубрика «📖 Слово дня» (раздел 7).

Термин публикуется, только если его определяет источник из конфига: промпт G проверяет, что определение
действительно есть в источнике, затем обычная проверка B и текст C по надстройке glossary. После публикации
термин попадает в словарь (SQLite, таблица Glossary в Notion, публичная страница).
"""

from __future__ import annotations

import html
import logging
import re
from datetime import timedelta
from typing import Any

from . import prompts
from .board import BoardUnavailable, GlossaryEntry
from .config import GlossaryTerm
from .llm import BudgetExceeded, LLMError
from .models import Candidate, GlossaryResult, PostRecord, Status
from .runtime import Runtime
from .steps import doc_guards, verify, write
from .timeutil import iso, week_start

log = logging.getLogger(__name__)
CONTOUR = "glossary"
TITLE_PREFIX = "Слово дня: "


def term_candidate(t: GlossaryTerm) -> Candidate:
    return Candidate(source="glossary", source_type="glossary", url=t.source, title=f"{TITLE_PREFIX}{t.term}",
                     extra={"term": t.term})


def _gkeys(t: GlossaryTerm) -> list[str]:
    return [f"g|{t.term.lower()}"]


_TAG_RE = re.compile(r"<[^>]+>")
_HASHTAG_RE = re.compile(r"#\w+")


def visible_text(post_html: str) -> str:
    """Что видит читатель: без тегов (и адресов ссылок внутри них) и без хэштегов."""
    return _HASHTAG_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", post_html))).lower()


def mentions(text: str, word: str) -> bool:
    """Слово целиком: «rag» не находится в «storage», «hook» — в «webhook»."""
    return re.search(rf"(?<!\w){re.escape(word.lower())}(?!\w)", text) is not None


def done_terms(rt: Runtime) -> set[str]:
    """Термины, которые уже вышли или могли выйти. Источники: SQLite, таблица Glossary в Notion и посты
    «Слова дня» в статусах «Отправляется»/«Ошибка» (неизвестно, вышли ли они) — потеря ветки state
    не приводит к повтору термина."""
    done = {r["term"].lower() for r in rt.state.glossary_entries()}
    done |= {e.term.lower() for e in rt.board.glossary_entries()}
    for st in (Status.SENDING, Status.ERROR):  # опубликованные уже есть в таблице Glossary (on_published)
        done |= {p.title.removeprefix(TITLE_PREFIX).strip().lower() for p in rt.board.posts_with_status(st)
                 if p.rubric == "glossary"}
    return done


def pick_terms(rt: Runtime) -> list[GlossaryTerm]:
    """Ещё не опубликованные термины: сначала встретившиеся в постах этой недели, потом стартовый список."""
    done = done_terms(rt)
    now = rt.now()
    todo = [t for t in rt.cfg.glossary.terms
            if t.term.lower() not in done and not rt.state.seen_outcome(_gkeys(t), now, 14)]
    try:
        local_now = rt.now_local()
        monday = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local_now.weekday())
        texts = " ".join(visible_text(f"{p.title} {p.html}") for p in rt.board.published_since(monday)
                         if p.rubric != "glossary")
    except BoardUnavailable:
        texts = ""
    mentioned = [t for t in todo if any(mentions(texts, w) for w in [t.term, *t.aliases])]
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
    terms = pick_terms(rt)
    if not terms:
        summary["status"] = "no_terms_left"
        week = week_start(rt.today()).isoformat()
        if rt.state.get("glossary:no_terms_week") != week:  # напоминаем раз в неделю, а не каждый день
            rt.state.put("glossary:no_terms_week", week)
            rt.notifier.notify("«Слово дня»: нет доступных терминов — все из glossary.terms (config.yaml) уже "
                               "вышли или отклонены за последние 14 дней. Добавьте новые термины с источниками, "
                               "иначе недельная квота словаря не выполнится")
        return summary
    for t in terms[:2]:  # не больше двух попыток за запуск
        result = make_term_post(rt, t)
        summary[result] = summary.get(result, 0) + 1
        if result in ("queued", "pending_approval"):
            break
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
        source_domain=c.domain, found_at=now, html=w.html, candidate_id=c.id,
        # для словаря — только определение: утверждения G, подтверждённые проверкой B (без примера и «не путать»)
        verify={**v.result.model_dump(), "definition": [x for x in g.claims if x in v.approved]}
        if v.result else None,
    ))
    rt.state.mark_seen(_gkeys(t), c.id, "queued", now)
    rt.write_out(f"posts/glossary-{c.id}.html", w.html)
    decision = "queued" if status == Status.APPROVED else "pending_approval"
    rt.decision(CONTOUR, c, "board", decision, [], rubric="glossary")
    return decision


def on_published(rt: Runtime, post: PostRecord, message_id: int, post_url: str | None) -> None:
    """После выхода поста: термин — в словарь (SQLite, Notion), страница словаря пересобирается."""
    term = post.title.removeprefix(TITLE_PREFIX).strip()
    definition = " ".join((post.verify or {}).get("definition") or [])[:1900]
    now = rt.now()
    rt.state.add_glossary(term=term, source_url=post.source_url, definition=definition, published_at=now,
                          post_url=post_url)
    try:
        rt.board.add_glossary(GlossaryEntry(term=term, definition=definition, source_url=post.source_url,
                                            published_at=now, post_url=post_url))
    except BoardUnavailable as e:
        rt.notifier.notify(f"термин «{term}» опубликован, но не записан в Notion: {e}")
    if rt.glossary_page is not None:
        sync_page(rt)


def all_entries(rt: Runtime) -> list[dict[str, Any]]:
    """Словарь целиком: таблица Glossary в Notion плюс SQLite (по термину; SQLite свежее)."""
    merged: dict[str, dict[str, Any]] = {}
    for e in rt.board.glossary_entries():
        merged[e.term.lower()] = {**e.model_dump(), "published_at": iso(e.published_at)}
    for r in rt.state.glossary_entries():
        merged[str(r["term"]).lower()] = dict(r)
    return list(merged.values())


def sync_page(rt: Runtime) -> None:
    """Пересобирает публичную страницу. Без чтения Notion не пересобираем: после потери состояния
    в SQLite не все термины, и страница бы их потеряла."""
    try:
        entries = all_entries(rt)
        url, dropped = rt.glossary_page.sync(entries)
        log.info("страница словаря обновлена: %s", url)
        if dropped:
            rt.notifier.notify(f"страница словаря упёрлась в лимит Telegraph: не показаны {dropped} самых старых "
                               "терминов (в Notion они есть)")
    except BoardUnavailable as e:
        rt.notifier.notify(f"страница словаря не обновлена — Notion не читается: {e}")
    except Exception as e:  # страница словаря не должна ломать публикацию
        rt.notifier.notify(f"не удалось обновить страницу словаря: {type(e).__name__}: {str(e)[:200]}")
