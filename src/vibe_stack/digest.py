"""Сводка дня админу (фаза 3): что вышло, что в очереди, что ждёт вашего решения, расход токенов, сбои.

Отправляется в личку ADMIN_CHAT_ID раз в день (cron в .github/workflows/digest.yml, 23:45 МСК). Все числа
считает код по состоянию и Notion, модель не участвует. Пауза сводку не останавливает: это служебное сообщение.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .board import BoardUnavailable
from .models import Status
from .runtime import Runtime
from .timeutil import local_date
from .weekly import reason_label

CONTOUR = "digest"


def digest_day(rt: Runtime) -> date:
    """День сводки: запуск в 23:45 или с опозданием после полуночи относится к одному и тому же дню."""
    return local_date(rt.now() - timedelta(hours=2), rt.tz)


def _n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def build_digest(rt: Runtime, day: date) -> str:
    lines = [f"📊 Сводка за {day:%d.%m}"]
    rubrics = rt.cfg.rubrics
    try:
        settings = rt.settings()
        if settings.pause:
            lines.append("⏸ В Notion включена «Пауза» — публикаций нет")
        limit = settings.regular_per_day if settings.regular_per_day is not None else rt.cfg.limits.regular_per_day
    except BoardUnavailable as e:
        lines.append(f"⚠️ Notion не читается: {e}")
        limit = rt.cfg.limits.regular_per_day

    # опубликовано
    pub = [p for p in rt.state.published_since(day)
           if p.local_date == day and p.rubric not in ("weekly", "watchlist", "ratings")]
    regular = [p for p in pub if not p.urgent]
    urgent = [p for p in pub if p.urgent]
    lines += ["", f"Вышло: {len(regular)} из {limit} возможных" + (f", срочных {len(urgent)}" if urgent else "")]
    if regular:
        by = Counter(p.rubric for p in regular)
        lines.append("  " + ", ".join(f"{rubrics[k].title if k in rubrics else k} — {v}" for k, v in by.most_common()))

    # очередь и решения за вами
    try:
        approved = [p for p in rt.board.posts_with_status(Status.APPROVED) if not p.urgent]
        pending = rt.board.posts_with_status(Status.PENDING)
        errors = rt.board.posts_with_status(Status.ERROR) + rt.board.posts_with_status(Status.SENDING)
        lines.append(f"В очереди: {len(approved)} одобренных")
        if pending:
            lines.append(f"Ждут вашего одобрения в Notion: {len(pending)}")
            lines += [f"  • {p.title[:80]}" for p in pending[:5]]
        if errors:
            lines.append(f"⚠️ «Ошибка»/«Отправляется» в Notion: {len(errors)} — проверьте")
    except BoardUnavailable as e:
        lines.append(f"⚠️ очередь не прочитать: {e}")

    # сбор
    found = [r for r in rt.state.candidates_between(day, day) if r["contour"] == "collect"
             and r["stage"] not in ("dedup", "deferred")]
    if found:
        ids = {r["id"] for r in found}
        queued = {r["id"] for r in found if r["decision"] in ("queued", "pending_approval")}
        reasons: Counter[str] = Counter()
        for r in found:
            if r["decision"] == "rejected":
                for code in json.loads(r["reasons"]) or ["other"]:
                    reasons[reason_label(code)] += 1
        lines += ["", f"Сбор: просмотрено {len(ids)}, в очередь {len(queued)}"]
        if reasons:
            lines.append("  отказы: " + ", ".join(f"{k} — {v}" for k, v in reasons.most_common(3)))
    broken: dict[str, str] = {}
    since = datetime.combine(day, time(0), tzinfo=ZoneInfo(rt.tz)).astimezone(UTC)  # начало дня сводки
    for run in rt.state.runs_since("collect", since):
        summary = json.loads(run["summary"] or "{}")
        broken.update({str(k): str(v)[:80] for k, v in (summary.get("source_errors") or {}).items()})
    if broken:
        lines.append("⚠️ Источники с ошибкой: " + "; ".join(f"{k}: {v}" for k, v in list(broken.items())[:5]))

    # песочница
    done = [r for r in rt.state.sandbox_since(since) if r["status"] in ("ok", "failed")]
    if done:
        ok = sum(1 for r in done if r["status"] == "ok")
        lines.append(f"🧪 Песочница: запущено {ok} из {len(done)}")

    # токены (сутки UTC — как считает лимит)
    day_start = rt.now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for model, cap in rt.cfg.llm.daily_token_limits.items():
        used = rt.llm.state.llm_tokens_since(model, day_start)
        lines.append(f"Токены {model} (сутки UTC): {_n(used)} из {_n(cap)} ({used * 100 // max(cap, 1)}%)")
    return "\n".join(lines)


def run_digest(rt: Runtime) -> dict[str, Any]:
    summary: dict[str, Any] = {"contour": CONTOUR, "mode": rt.mode}
    if not rt.cfg.digest.enabled:
        summary["status"] = "disabled"
        return summary
    day = digest_day(rt)
    key = f"digest:{day.isoformat()}"
    if rt.already_done(key):
        summary["status"] = "already_sent"
        return summary
    text = build_digest(rt, day)
    rt.notifier.notify(text)
    rt.write_out("digest.txt", text)
    rt.mark_done(key)
    summary.update(status="sent", day=day.isoformat())
    return summary
