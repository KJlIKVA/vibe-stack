"""Фикстуры из tests/fixtures/*.yaml и прогон сценариев раздела 12 в изоляции.

Каждая фикстура описывает вход (кандидат, документ источника, состояние) и ожидаемое решение.
Ответы модели можно взять из фикстуры (--fake-llm) или получить от настоящей модели (eval).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .board import LocalBoard
from .config import Config
from .fetch import FixtureFetcher
from .llm import LLM, FakeLLM
from .models import Candidate, PostRecord, Status
from .storage import State
from .telegram import DryRunTelegram, Notifier
from .timeutil import local_date
from .urls import canonical_url


@dataclass
class Fixture:
    id: str
    description: str
    scenario: str
    expected: dict[str, Any]
    candidates: list[Candidate]
    documents: dict[str, dict[str, Any]]
    llm: dict[tuple[str, str], Any]
    preseed: dict[str, Any] = field(default_factory=dict)
    board: dict[str, Any] = field(default_factory=dict)
    phase: int = 1
    path: Path | None = None


def _dt(now: datetime, days: float | None, hours: float | None = None) -> datetime | None:
    if days is None and hours is None:
        return None
    return now - timedelta(days=days or 0, hours=hours or 0)


def load_fixture(path: Path, now: datetime) -> Fixture:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cands: list[Candidate] = []
    docs: dict[str, dict[str, Any]] = {}
    llm: dict[tuple[str, str], Any] = {}
    for item in raw.get("candidates", []):
        c = Candidate(
            source=item.get("source", "fixture"),
            source_type=item.get("source_type", "rss"),
            url=item["url"],
            title=item["title"],
            summary=item.get("summary", ""),
            published_at=_dt(now, item.get("published_days_ago"), item.get("published_hours_ago")),
            updated_at=_dt(now, item.get("updated_days_ago"), item.get("updated_hours_ago")),
            whitelist=item.get("whitelist", False),
            official_domains=item.get("official_domains", []),
            signal=item.get("signal", 1.0),
            extra=item.get("extra", {}),
        )
        cands.append(c)
        if "document" in item:
            docs[canonical_url(c.url)] = item["document"]
        for step, payload in (item.get("llm") or {}).items():
            ctx = c.id + ("#retry" if step.endswith("_retry") else "")
            if step in ("score", "triage") and isinstance(payload, dict):
                payload = {"id": c.id, **payload}  # id кандидата вычисляется из URL
            llm[(step.removesuffix("_retry"), ctx)] = payload
    for step, payload in (raw.get("llm_global") or {}).items():
        llm[(step, step)] = payload
    return Fixture(
        id=str(raw["id"]), description=raw.get("description", ""), scenario=raw.get("scenario", "collect"),
        expected=raw.get("expected", {}), candidates=cands, documents=docs, llm=llm,
        preseed=raw.get("preseed") or {}, board=raw.get("board") or {}, phase=int(raw.get("phase", 1)), path=path,
    )


def load_fixtures(directory: str | Path, now: datetime, max_phase: int = 1) -> list[Fixture]:
    out = [load_fixture(p, now) for p in sorted(Path(directory).glob("*.yaml"))]
    return [f for f in out if f.phase <= max_phase]


# --- прогон сценариев ---------------------------------------------------------------------------

LLMFactory = Callable[[Config, State, str, Callable[[], datetime], str, Fixture], LLM]


def fake_llm_factory(cfg: Config, state: State, run_id: str, clock: Callable[[], datetime], tz: str,
                     fx: Fixture) -> LLM:
    return FakeLLM(cfg.llm, state, run_id, clock, tz, fx.llm)


@dataclass
class ScenarioResult:
    fixture: Fixture
    actual: dict[str, Any]
    ok: bool
    mismatches: list[str]
    llm_calls: list[tuple[str, str, str]] = field(default_factory=list)
    board: LocalBoard | None = None
    sent: list[tuple[str, str]] = field(default_factory=list)


def run_scenario(fx: Fixture, cfg: Config, now: datetime, llm_factory: LLMFactory = fake_llm_factory,
                 workdir: Path | None = None) -> ScenarioResult:
    from .collect import run_collect
    from .publish import run_publish
    from .runtime import Runtime
    from .urgent import run_urgent

    tmp = Path(workdir or tempfile.mkdtemp(prefix=f"vs-{fx.id}-"))
    state = State(tmp / "state.db")
    board = LocalBoard(tmp / "board.json")
    if fx.board.get("settings"):
        board.set_settings(**fx.board["settings"])
    for key, ov in (fx.board.get("rubrics") or {}).items():
        from .board import RubricOverride

        board.data.rubrics[key] = RubricOverride(**ov)
    for p in fx.board.get("posts") or []:
        fields = {k: v for k, v in p.items() if k != "found_hours_ago"}
        board.add_post(PostRecord(**fields, found_at=now - timedelta(hours=p.get("found_hours_ago", 1))))
    tz = cfg.channel.tz
    today = local_date(now, tz)
    for c in fx.candidates:
        if fx.preseed.get("published"):
            state.mark_seen(c.keys, c.id, "published", now - timedelta(days=3))
    for i in range(int(fx.preseed.get("urgent_published_today", 0))):
        state.record_published(ref=f"pre-u{i}", rubric="urgent", urgent=True, title=f"earlier urgent {i}",
                               source_url=f"https://example.org/u{i}", domain="example.org",
                               published_at=now - timedelta(minutes=30 * (i + 1)), day=today, slot=None,
                               tg_message_id=100 + i, counts_regular=False)

    tg = DryRunTelegram(tmp / "telegram")
    clock = lambda: now  # noqa: E731
    run_id = f"fixture-{fx.id}"
    llm = llm_factory(cfg, state, run_id, clock, tz, fx)
    rt = Runtime(
        cfg=cfg, state=state, board=board, tg=tg, notifier=Notifier(None, None, tmp / "admin.log"), llm=llm,
        fetcher=FixtureFetcher(fx.documents, clock, cfg.fetch.max_doc_chars), clock=clock, run_id=run_id,
        out_dir=tmp, mode="dry-run", channel_id="@vibe_stack_test", sources_factory=lambda _: [],
        fixture_candidates=fx.candidates,
    )
    state.start_run(run_id, fx.scenario, "dry-run", now)
    actual: dict[str, Any] = {}
    match fx.scenario:
        case "collect":
            actual["summary"] = run_collect(rt)
        case "urgent":
            actual["summary"] = run_urgent(rt)
        case "publish":
            actual["summary"] = run_publish(rt)
        case "approve_flow":
            actual["collect"] = run_collect(rt)
            rt._settings = None
            actual["publish_before"] = run_publish(rt)
            for p in board.posts_with_status(Status.PENDING):
                board.update_post(p.ref, status=Status.APPROVED)  # человек одобрил в Notion
            rt._settings = None
            actual["publish_after"] = run_publish(rt)
        case "pause":
            actual["publish"] = run_publish(rt)
            rt._settings = None
            actual["urgent"] = run_urgent(rt)
        case "empty_day":
            actual["collect"] = run_collect(rt)
            rt._settings = None
            actual["publish"] = run_publish(rt)
        case _:
            raise ValueError(f"неизвестный сценарий {fx.scenario}")

    rows = state.db.execute("SELECT * FROM candidates ORDER BY rowid").fetchall()
    actual["decisions"] = [
        {"id": r["id"], "stage": r["stage"], "decision": r["decision"], "reasons": r["reasons"],
         "rubric": r["rubric"]} for r in rows
    ]
    actual["sent"] = len(tg.sent)
    actual["board_statuses"] = [p.status.value for p in board.data.posts]
    mismatches = check_expected(fx, actual)
    calls = getattr(llm, "calls", [])
    state.close()
    return ScenarioResult(fx, actual, not mismatches, mismatches, calls, board, tg.sent)


def check_expected(fx: Fixture, actual: dict[str, Any]) -> list[str]:
    """expected: decision/stage/reasons_include/rubric/sent/board_status/summary.{k}."""
    exp = fx.expected
    out: list[str] = []
    decisions = actual["decisions"]
    last = decisions[-1] if decisions else None
    if "decision" in exp:
        got = last["decision"] if last else None
        if got != exp["decision"]:
            out.append(f"decision: ждали {exp['decision']}, получили {got}")
    if "stage" in exp and (last["stage"] if last else None) != exp["stage"]:
        out.append(f"stage: ждали {exp['stage']}, получили {last['stage'] if last else None}")
    if "rubric" in exp and (last["rubric"] if last else None) != exp["rubric"]:
        out.append(f"rubric: ждали {exp['rubric']}, получили {last['rubric'] if last else None}")
    for reason in exp.get("reasons_include", []):
        if not last or reason not in last["reasons"]:
            out.append(f"reasons: нет {reason} в {last['reasons'] if last else None}")
    if "sent" in exp and actual["sent"] != exp["sent"]:
        out.append(f"sent: ждали {exp['sent']}, отправлено {actual['sent']}")
    if "board_statuses" in exp and actual["board_statuses"] != exp["board_statuses"]:
        out.append(f"board: ждали {exp['board_statuses']}, получили {actual['board_statuses']}")
    for path, want in (exp.get("summary") or {}).items():
        node: Any = actual
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node != want:
            out.append(f"{path}: ждали {want!r}, получили {node!r}")
    if exp.get("no_decisions") and decisions:
        out.append(f"ждали пустой журнал, получили {len(decisions)} решений")
    return out
