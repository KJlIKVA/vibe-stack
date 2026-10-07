"""Шаги пайплайна, общие для контуров: prefilter, кодовые проверки, гейт, проверка B, текст C."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import prompts
from .config import Config, Rubric
from .guards import affiliate_reason, domain_in, find_injection, only_unsafe_install
from .lint import lint_post
from .models import CATEGORY_TO_RUBRIC, Candidate, FetchedDoc, ScoreResult, VerifyResult
from .runtime import Runtime
from .timeutil import iso, parse_dt

log = logging.getLogger(__name__)

# порядок предпочтения ссылки, когда одна новость пришла из нескольких источников
_SOURCE_RANK = {"rss": 0, "sitemap": 0, "github_releases": 0, "github_search": 1, "hackernews": 2}


def merge_batch(cands: list[Candidate]) -> list[Candidate]:
    """Одна новость из нескольких источников → один кандидат с лучшей ссылкой."""
    groups: list[list[Candidate]] = []
    key_to_group: dict[str, int] = {}
    for c in cands:
        idx = next((key_to_group[k] for k in c.keys if k in key_to_group), None)
        if idx is None:
            idx = len(groups)
            groups.append([])
        groups[idx].append(c)
        for k in c.keys:
            key_to_group.setdefault(k, idx)
    out = []
    for g in groups:
        best = min(g, key=lambda c: (0 if c.whitelist else 1, _SOURCE_RANK.get(c.source_type, 3), -c.signal))
        if len(g) > 1:
            best = best.model_copy(update={
                "signal": max(c.signal for c in g),
                "extra": {**best.extra, "also_from": sorted({c.source for c in g if c is not best})},
            })
        out.append(best)
    return out


def prefilter(c: Candidate, cfg: Config, now: datetime) -> list[str]:
    """Дешёвые проверки кодом до любой загрузки и LLM."""
    reasons = []
    if not c.title.strip():
        reasons.append("no_title")
    if domain_in(c.url, cfg.prefilter.blocked_domains):
        reasons.append("blocked_domain")
    if domain_in(c.url, cfg.prefilter.pirate_domains):
        reasons.append("ad")
    if aff := affiliate_reason(c.url, cfg.prefilter.affiliate_params):
        reasons.append("ad")
        log.debug("affiliate: %s", aff)
    fresh = c.freshest
    if fresh is not None and now - fresh > timedelta(days=cfg.gate.max_age_days):
        reasons.append("outdated")
    if cfg.collect.code_injection_guard and find_injection(f"{c.title}\n{c.summary}"):
        reasons.append("injection_detected")
    return sorted(set(reasons))


_SITE_SUFFIX = re.compile(r"\s+[\\|—–-]\s+[^\\|—–-]{1,30}$")


def with_page_title(c: Candidate, doc: FetchedDoc) -> Candidate:
    """У sitemap вместо заголовка только slug — берём заголовок со страницы (без « \\ Сайт»)."""
    if c.source_type != "sitemap" or not doc.title.strip():
        return c
    return c.model_copy(update={"title": _SITE_SUFFIX.sub("", doc.title.strip())})


def doc_guards(doc: FetchedDoc, cfg: Config, now: datetime | None = None) -> list[str]:
    """Проверки кодом по загруженному первоисточнику (now — проверять и даты документа)."""
    reasons = []
    if cfg.collect.code_injection_guard and find_injection(doc.text):
        reasons.append("injection_detected")
    if only_unsafe_install(doc.text):
        reasons.append("unsafe_install")
    if now is not None:
        dates = [d for d in (parse_dt(doc.published_meta), parse_dt(doc.updated_meta)) if d]
        if dates and now - max(dates) > timedelta(days=cfg.gate.max_age_days):
            reasons.append("outdated")  # по датам самого источника: старый проект, всплывший на HN
    return reasons


@dataclass
class GateResult:
    rubric: str | None
    total: int
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons


def gate(score: ScoreResult, cfg: Config, rubrics: dict[str, Rubric]) -> GateResult:
    """Сумма и пороги — только здесь, в коде."""
    total = score.scores.total
    reasons: list[str] = list(score.hard_stops)
    rubric = CATEGORY_TO_RUBRIC.get(score.category)
    if rubric is None:
        reasons.append("off_topic")
    elif not rubrics.get(rubric, Rubric(emoji="", hashtag="", title="", enabled=False)).enabled:
        reasons.append("rubric_disabled")
    if total < cfg.gate.min_total:
        reasons.append("low_score")
    if score.scores.verifiability < cfg.gate.min_verifiability:
        reasons.append("low_verifiability")
    return GateResult(rubric, total, sorted(set(reasons), key=reasons.index))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".")


@dataclass
class VerifyOutcome:
    result: VerifyResult | None
    approved: list[str]
    reasons: list[str]

    @property
    def passed(self) -> bool:
        return not self.reasons


def verify(rt: Runtime, c: Candidate, claims: list[str], mode: str) -> VerifyOutcome:
    """Проверка B: свежая загрузка источника и отдельный вызов без черновика и оценок."""
    doc = rt.fetcher.fetch(c.url, purpose="verify")
    if not doc.ok:
        return VerifyOutcome(None, [], [f"verify_source_unavailable:{doc.error}"])
    if guards := doc_guards(doc, rt.cfg):
        return VerifyOutcome(None, [], guards)
    meta = {"url": c.url, "fetched_at": iso(doc.fetched_at), "http_status": doc.http_status, "mode": mode}
    res: VerifyResult = rt.llm.json("verify", prompts.verify_prompt(claims, doc.text, meta), ctx_id=c.id)
    # код не верит списку approved_claims на слово: берём только supported и только из исходных утверждений
    originals = {_norm(x): x for x in claims}
    approved = []
    for ch in res.checks:
        if ch.status == "supported" and _norm(ch.claim) in originals:
            orig = originals[_norm(ch.claim)]
            if orig not in approved:
                approved.append(orig)
    reasons = []
    if res.verdict != "pass":
        reasons.append("verify_fail" + (f":{res.fail_reason[:120]}" if res.fail_reason else ""))
    if len(approved) < rt.cfg.gate.min_approved_claims:
        reasons.append(f"approved_claims<{rt.cfg.gate.min_approved_claims}")
    return VerifyOutcome(res, approved, reasons)


@dataclass
class WriteOutcome:
    html: str | None
    errors: list[str]


def write(rt: Runtime, c: Candidate, rubric_key: str, rubric: Rubric, approved: list[str],
          mode: str) -> WriteOutcome:
    """Текст C + lint; при ошибках линтера — одна попытка исправить."""
    meta = {"category": rubric_key, "url": c.url, "title": c.title, "mode": mode}
    prompt = prompts.write_prompt(approved, meta, rubric.overlay, rubric.emoji, rubric.hashtag)
    template = prompts.load(prompts.OVERLAY_FILES[rubric.overlay])
    # заголовок — тоже данные из интернета: числа в посте только из подтверждённых утверждений
    allowed = "\n".join(approved)

    def check(text: str) -> list[str]:
        return lint_post(text, rubric=rubric_key, max_chars=rubric.max_chars, source_url=c.url,
                         allowed_text=allowed, template_text=template)

    html = rt.llm.text("write", prompt, ctx_id=c.id)
    errors = check(html)
    if errors:
        log.info("lint %s: %s — прошу исправить", c.id, errors)
        retry = (prompt + "\n\nПредыдущий вариант поста не прошёл автоматическую проверку: "
                 + ", ".join(errors) + "\n<previous_draft>\n" + prompts.neutralize(html) + "\n</previous_draft>\n"
                 "Исправь эти ошибки, соблюдая все правила выше, и верни только текст поста.")
        html = rt.llm.text("write", retry, ctx_id=f"{c.id}#retry")
        errors = check(html)
    return WriteOutcome(None if errors else html, errors)
