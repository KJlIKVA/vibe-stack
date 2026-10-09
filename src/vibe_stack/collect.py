"""Контур «Сбор»: источники → дедуп → prefilter → fetch → A → гейт → B → C → lint → доска."""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import timedelta
from typing import Any

from . import dayplan, images, prompts, sandbox
from .board import BoardUnavailable
from .config import Rubric
from .llm import BudgetExceeded, LLMError
from .models import HARD_STOPS, Candidate, PostRecord, ScoreResult, Status
from .runtime import Runtime
from .sources import collect_all
from .steps import _SOURCE_RANK, doc_guards, gate, merge_batch, prefilter, verify, with_page_title, write

log = logging.getLogger(__name__)
CONTOUR = "collect"
FEED_TYPES = ("rss", "sitemap", "github_releases", "md_changelog", "youtube", "jsonld_list", "openlibrary")


def run_collect(rt: Runtime) -> dict[str, Any]:
    summary: dict[str, Any] = {"contour": CONTOUR, "mode": rt.mode, "found": 0, "new": 0, "processed": 0,
                               "queued": 0, "pending_approval": 0, "rejected": 0, "fetch_failed": 0,
                               "errors": 0, "source_errors": {}, "stopped": None}
    now = rt.now()
    # два сбора в день (решение 48): утренний (до полудня по времени канала) и дневной — днём выходят новости США
    half = collect_half(rt)
    summary["half"] = half
    done_key = f"collect:{rt.today().isoformat()}:{half}"
    if rt.already_done(done_key):
        # запасной cron-запуск на случай, если первый отменила очередь Actions
        summary["status"] = "already_ran_today"
        return summary
    try:
        rt.settings()  # нужна только проверка, что доска читается: без неё некуда класть посты
    except BoardUnavailable as e:
        rt.notifier.notify(f"сбор остановлен: {e}")
        summary["stopped"] = str(e)
        return summary
    rubrics = rt.rubrics()

    raw, errors = collect_all(rt.sources(CONTOUR))
    summary["found"] = len(raw)
    summary["source_errors"] = errors
    if not raw:
        summary["reason"] = "нет кандидатов: источники ничего не вернули"
        log.info(summary["reason"])
        return summary

    passed: list[Candidate] = []
    ages = {s.name: s.max_age_days for s in rt.cfg.sources if s.max_age_days is not None}
    for c in merge_batch(raw):
        max_age = timedelta(days=ages.get(c.source, rt.cfg.gate.max_age_days))
        if c.source_type in FEED_TYPES and c.freshest and now - c.freshest > max_age:
            continue  # старый хвост фида — не кандидат, а архив
        if outcome := rt.state.seen_outcome(c.keys, now, rt.cfg.dedup.seen_ttl_days):
            if outcome == "published":
                rt.decision(CONTOUR, c, "dedup", "rejected", ["duplicate:published"])
            continue  # уже видели: в журнал не пишем, чтобы не раздувать статистику
        summary["new"] += 1
        if reasons := prefilter(c, rt.cfg, now):
            rt.decision(CONTOUR, c, "prefilter", "rejected", reasons)
            rt.state.mark_seen(c.keys, c.id, "rejected", now)
            summary["rejected"] += 1
            continue
        passed.append(c)

    passed = drop_repeated_topics(rt, passed)
    chosen, rest = fair_pick(passed, rt.cfg.collect.max_candidates_to_score, rt.cfg.collect.max_per_source)
    for c in rest:
        rt.decision(CONTOUR, c, "deferred", "deferred", ["over_max_candidates_to_score"])
    deadline = time.monotonic() + rt.cfg.collect.max_minutes * 60
    board_failed = False
    for c in chosen:
        if time.monotonic() > deadline:
            summary["stopped"] = f"дедлайн {rt.cfg.collect.max_minutes} мин — остальные кандидаты завтра"
            log.warning(summary["stopped"])
            break
        try:
            result = process_candidate(rt, c, rubrics)
        except BudgetExceeded as e:
            summary["stopped"] = str(e)
            rt.notifier.notify(f"сбор остановлен: {e}")
            break
        except BoardUnavailable as e:
            # доска не принимает записи — дальше тратить модель бессмысленно
            summary["errors"] += 1
            summary["stopped"] = str(e)
            rt.decision(CONTOUR, c, "error", "error", ["BoardUnavailable"], detail={"error": str(e)[:500]})
            rt.state.mark_seen(c.keys, c.id, "board_error", rt.now())
            rt.notifier.notify(f"сбор остановлен: {e}")
            board_failed = True
            break
        except LLMError as e:
            summary["errors"] += 1
            rt.decision(CONTOUR, c, "error", "error", ["LLMError"], detail={"error": str(e)[:500]})
            continue
        except Exception as e:
            # неожиданная ошибка на одном кандидате (странная страница) не роняет весь сбор: без этого не было бы
            # ни остальных постов, ни «Слова дня», ни плана дня
            log.exception("кандидат %s: ошибка обработки", c.url)
            summary["errors"] += 1
            rt.decision(CONTOUR, c, "error", "error", [type(e).__name__], detail={"error": str(e)[:500]})
            rt.state.mark_seen(c.keys, c.id, "error", rt.now())
            continue
        summary["processed"] += 1
        summary[result] += 1
    if summary["queued"] + summary["pending_approval"] == 0 and not summary["stopped"]:
        summary["reason"] = "сегодня ничего не прошло отбор"
        log.info("публикаций из сбора ноль: %s", summary["reason"])
    if not board_failed:
        # время каждому посту на сегодня + «План на сегодня» админу (после дневного сбора — обновлённый)
        summary["plan"] = dayplan.plan_and_report(rt, update=half == "pm")
    if not board_failed:  # при сбое Notion второй cron-запуск дня попробует ещё раз
        rt.mark_done(done_key)
    return summary


def drop_repeated_topics(rt: Runtime, cands: list[Candidate]) -> list[Candidate]:
    """Одна новость из разных источников (анонс, пересказ в блоге, пост на Reddit) оценивается один раз (решение 59).

    Новость уже в очереди, недавно вышла или есть у кандидата получше в этом же сборе — модель на повтор не
    тратим. Правило строже планировщика (same_news): лишняя оценка дешевле потерянной новости. Остаётся лучший:
    белый список и первоисточник раньше пересказа, затем сигнал источника. Видео, подкасты и книги не
    сравниваются: ролик о новости — отдельный пост «что посмотреть», а не повтор.
    """
    from .planner import same_news, topic_tokens

    cutoff = rt.now() - timedelta(days=rt.cfg.planner.topic_repeat_days)
    known = [topic_tokens(h.title, h.source_url) for h in rt.state.published_since(cutoff.date())]
    with contextlib.suppress(BoardUnavailable):  # без очереди сравним с вышедшим и внутри сбора
        known += [topic_tokens(p.title, p.source_url) for p in rt.board.posts_with_status(Status.APPROVED)]
    kept: list[Candidate] = []
    kept_topics: list[set[str]] = []
    for c in sorted(cands, key=lambda c: (0 if c.whitelist else 1, _SOURCE_RANK.get(c.source_type, 3), -c.signal)):
        if c.extra.get("media"):
            kept.append(c)
            continue
        topic = topic_tokens(c.title, c.url)
        if any(same_news(topic, t) for t in known + kept_topics):
            rt.decision(CONTOUR, c, "dedup", "rejected", ["duplicate_topic"])
            rt.state.mark_seen(c.keys, c.id, "rejected", rt.now())
            continue
        kept.append(c)
        kept_topics.append(topic)
    if dropped := len(cands) - len(kept):
        log.info("повторы тем до оценки: %d из %d", dropped, len(cands))
    return [c for c in cands if any(c is k for k in kept)]  # порядок прежний


def collect_half(rt: Runtime) -> str:
    """«am» — утренний сбор, «pm» — дневной (по времени канала)."""
    from zoneinfo import ZoneInfo

    return "am" if rt.now().astimezone(ZoneInfo(rt.tz)).hour < 12 else "pm"


def fair_pick(cands: list[Candidate], limit: int, per_source: int) -> tuple[list[Candidate], list[Candidate]]:
    """Источники по очереди, внутри источника — сначала самое свежее (по дню), потом по сигналу (звёзды, очки).
    Свежее не должно ждать, пока модель оценивает недельный хвост ленты (решение 59). Остальное ждёт
    следующего запуска."""
    def order(c: Candidate) -> tuple[int, float]:
        day = c.freshest.date().toordinal() if c.freshest else 10**7  # без даты (каталог) — как самое свежее
        return (-day, -c.signal)

    by_source: dict[str, list[Candidate]] = {}
    for c in sorted(cands, key=order):
        by_source.setdefault(c.source, []).append(c)
    chosen: list[Candidate] = []
    rnd = 0
    while len(chosen) < limit and rnd < per_source:
        took = False
        for items in by_source.values():
            if rnd < len(items) and len(chosen) < limit:
                chosen.append(items[rnd])
                took = True
        if not took:
            break
        rnd += 1
    picked = {id(c) for c in chosen}
    return chosen, [c for c in cands if id(c) not in picked]


def _reject(rt: Runtime, c: Candidate, stage: str, reasons: list[str], score: ScoreResult | None = None,
            rubric: str | None = None, total: int | None = None, verify_json: dict[str, Any] | None = None) -> str:
    rt.decision(CONTOUR, c, stage, "rejected", reasons, rubric=rubric, score_total=total)
    rt.state.mark_seen(c.keys, c.id, "rejected", rt.now())
    rt.board.add_post(PostRecord(
        title=c.title, rubric=rubric or "none", status=Status.REJECTED, score=total,
        scores=score.scores.model_dump() if score else None,
        hard_stops=[r for r in reasons if r in HARD_STOPS], reject_reason=", ".join(reasons)[:500],
        source_url=c.url, source_domain=c.domain, found_at=rt.now(), verify=verify_json, candidate_id=c.id,
    ))
    return "rejected"


def process_candidate(rt: Runtime, c: Candidate, rubrics: dict[str, Rubric]) -> str:
    doc = rt.fetcher.fetch(c.url, purpose="score")
    if not doc.ok:
        # временно недоступен — не помечаем просмотренным, попробуем в следующий раз
        rt.decision(CONTOUR, c, "fetch", "fetch_failed", ["source_unavailable"], detail={"error": doc.error})
        return "fetch_failed"
    c = with_page_title(c, doc)
    max_age = next((s.max_age_days for s in rt.cfg.sources if s.name == c.source), None)
    if guards := doc_guards(doc, rt.cfg, rt.now(), max_age, book=c.extra.get("media") == "book"):
        return _reject(rt, c, "code_guard", guards)

    score: ScoreResult = rt.llm.json("score", prompts.score_prompt(c.for_prompt(), doc.text, rt.today()),
                                       ctx_id=c.id)
    g = gate(score, rt.cfg, rubrics)
    # объяснение модели — в лог запуска: по нему видно, почему видео или статья «стоит» или «не стоит» поста
    log.info("оценка %s: %s, %s/15, %s — %s", c.url, score.category, g.total, ",".join(g.reasons) or "прошла",
             score.reason)
    if not g.passed:
        return _reject(rt, c, "gate", g.reasons, score, g.rubric, g.total)
    assert g.rubric is not None
    rubric = rubrics[g.rubric]

    v = verify(rt, c, score.claims, mode="standard")
    verify_json = v.result.model_dump() if v.result else None
    if not v.passed:
        return _reject(rt, c, "verify", v.reasons, score, g.rubric, g.total, verify_json)

    w = write(rt, c, g.rubric, rubric, v.approved, mode="standard")
    if w.html is None:
        return _reject(rt, c, "lint", ["lint:" + ";".join(w.errors)], score, g.rubric, g.total, verify_json)

    status = Status.APPROVED if rubric.mode == "auto" else Status.PENDING
    ref = rt.board.add_post(PostRecord(
        title=c.title, rubric=g.rubric, status=status, mode=rubric.mode, score=g.total,
        scores=score.scores.model_dump(), source_url=c.url, source_domain=c.domain, found_at=rt.now(),
        html=w.html, verify=verify_json, candidate_id=c.id,
    ))
    rt.state.mark_seen(c.keys, c.id, "queued", rt.now())
    images.remember(rt, c.id, doc.image)  # картинка поста — главная картинка первоисточника
    rt.write_out(f"posts/{c.id}.html", w.html)
    detail: dict[str, Any] = {"ref": ref}
    try:
        if req := sandbox.plan(rt, candidate_id=c.id, url=c.url, rubric=g.rubric, doc_text=doc.text):
            detail["sandbox"] = req
    except Exception as e:  # песочница — необязательная пометка, сбор из-за неё не падает
        log.warning("песочница: заявка для %s не создана: %s", c.id, type(e).__name__)
    decision = "queued" if status == Status.APPROVED else "pending_approval"
    rt.decision(CONTOUR, c, "board", decision, [], rubric=g.rubric, score_total=g.total, detail=detail)
    return decision

