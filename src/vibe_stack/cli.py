"""Командная строка: `vibe-stack <контур> [--dry-run | --publish]`.

По умолчанию --dry-run: ничего не публикует и не пишет в Notion, результаты — в out/<run_id>/.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from .board import Board, BoardUnavailable, LocalBoard, RecordingBoard
from .config import Config, MissingSecret, env, load_config, load_dotenv
from .fetch import HttpFetcher
from .llm import LLM, NoLLM, OpenAILLM
from .logs import setup_logging
from .runtime import Runtime
from .sandbox import Registry
from .sources import build_source
from .storage import State
from .telegram import DryRunTelegram, Notifier, Telegram
from .timeutil import local_date, parse_dt, utc_now

log = logging.getLogger("vibe_stack")
CONTOURS = ("collect", "publish", "urgent", "weekly", "pin", "glossary", "digest")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vibe-stack", description="Пайплайн Telegram-канала Vibe Stack")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default="state/vibe_stack.db", help="SQLite-состояние (ветка state)")
    p.add_argument("--out", default="out")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    for name in CONTOURS:
        sp = sub.add_parser(name, help=f"контур {name}")
        mode = sp.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="по умолчанию: ничего не публиковать")
        mode.add_argument("--publish", action="store_true", help="публиковать по-настоящему")
        sp.add_argument("--board", choices=("auto", "local", "notion"), default="auto")
        sp.add_argument("--local-board", default="out/local_board.json")
        sp.add_argument("--now", help="подменить текущее время (ISO 8601), для отладки")
        sp.add_argument("--force", action="store_true",
                        help="запустить, даже если сбор/итоги за этот период уже были (только вручную)")
    ev = sub.add_parser("eval", help="прогнать фикстуры раздела 12 и сверить решения")
    ev.add_argument("--fixtures", default="tests/fixtures")
    ev.add_argument("--llm", choices=("fake", "real"), default="fake",
                    help="fake — ответы из фикстур; real — настоящая модель из config.yaml")
    ev.add_argument("--phase", type=int, default=2)
    ev.add_argument("--only", help="id фикстуры")
    sub.add_parser("notion-setup", help="создать базы Notion под NOTION_ROOT_PAGE_ID")
    sub.add_parser("telegraph-setup", help="один раз создать страницу словаря на Telegraph (токен — в .env)")
    sub.add_parser("status", help="счётчики за сегодня и расходы")
    se = sub.add_parser("sandbox-export", help="заявки песочницы в JSON (для job без секретов)")
    se.add_argument("--out-file", required=True)
    se.add_argument("--test", default="", help="проверочная заявка без состояния: pypi:<пакет> или npm:<пакет>")
    ar = sub.add_parser("admin-report", help="отправить отчёт админу в личку Telegram (файл в Telegram HTML)")
    ar.add_argument("file")
    sa = sub.add_parser("sandbox-apply", help="записать результаты песочницы в состояние")
    sa.add_argument("results")
    sa.add_argument("--requests", required=True, help="requests.json этого запуска (от job plan)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    load_dotenv()
    cfg = load_config(args.config)
    try:
        if args.command in CONTOURS:
            code = run_contour(args, cfg)
        elif args.command == "eval":
            code = run_eval(args, cfg)
        elif args.command == "notion-setup":
            code = notion_setup(cfg)
        elif args.command == "telegraph-setup":
            code = telegraph_setup(cfg)
        elif args.command == "sandbox-export":
            code = sandbox_export(args, cfg)
        elif args.command == "sandbox-apply":
            code = sandbox_apply(args, cfg)
        elif args.command == "admin-report":
            code = admin_report(args)
        else:
            code = status(args, cfg)
    except MissingSecret as e:
        print(f"Ошибка: {e}. Задайте её в .env (локально) или в GitHub Secrets.", file=sys.stderr)
        code = 2
    sys.exit(code)


# --- контуры ---------------------------------------------------------------------------

def _notion_board(cfg: Config) -> Board:
    from .notion_board import NotionBoard

    token = env("NOTION_TOKEN", required=True)
    root = env("NOTION_ROOT_PAGE_ID", required=True)
    assert token and root
    return NotionBoard(token, root, cfg)


def build_runtime(args: argparse.Namespace, cfg: Config, contour: str) -> Runtime:
    publish = bool(args.publish)
    mode = "publish" if publish else "dry-run"
    fixed_now = parse_dt(args.now) if args.now else None
    clock = (lambda: fixed_now) if fixed_now else utc_now
    run_id = f"{contour}-{clock().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    out_dir = Path(args.out) / run_id
    setup_logging(out_dir, args.verbose)

    state_path = Path(args.state)
    # расходы на модель пишем в настоящий журнал даже в dry-run: деньги тратятся по-настоящему
    ledger = State(state_path)
    if not publish:
        # dry-run работает на копии состояния: настоящие счётчики и «просмотренное» не трогаем
        copy = out_dir / "state.db"
        copy.parent.mkdir(parents=True, exist_ok=True)
        if state_path.exists():
            shutil.copy(state_path, copy)
        state_path = copy
    state = State(state_path)

    use_notion = args.board == "notion" or (args.board == "auto" and env("NOTION_TOKEN") and env("NOTION_ROOT_PAGE_ID"))
    if publish and not use_notion and args.board != "local":
        raise MissingSecret("NOTION_TOKEN / NOTION_ROOT_PAGE_ID (для --publish нужна доска в Notion)")
    if use_notion:
        board: Board = _notion_board(cfg)
        if not publish:
            board = RecordingBoard(board, out_dir / "board_writes.jsonl")
    else:
        board = LocalBoard(args.local_board)
        log.info("доска: локальный файл %s", args.local_board)

    if publish:
        tg: Telegram | DryRunTelegram = Telegram(env("TELEGRAM_BOT_TOKEN", required=True) or "")
        channel = env("TELEGRAM_CHANNEL_ID", required=True) or ""
        notifier = Notifier(tg, env("ADMIN_CHAT_ID"), out_dir / "admin.log")
        if not env("ADMIN_CHAT_ID"):
            log.warning("ADMIN_CHAT_ID не задан — оповещения только в лог")
    else:
        tg = DryRunTelegram(out_dir / "telegram")
        channel = env("TELEGRAM_CHANNEL_ID") or "@dry_run"
        notifier = Notifier(None, None, out_dir / "admin.log")

    http = httpx.Client(timeout=cfg.fetch.timeout_s, headers={"User-Agent": cfg.fetch.user_agent})
    glossary_page = None
    if cfg.glossary.telegraph_page and cfg.glossary.telegraph_path:
        from .glossary_page import DryRunPage, TelegraphPage

        token = env("TELEGRAPH_TOKEN")
        if not publish:
            glossary_page = DryRunPage(out_dir / "glossary_page.json")
        elif token:
            author = f"https://t.me/{cfg.channel.username}" if cfg.channel.username else ""
            glossary_page = TelegraphPage(token, cfg.glossary.telegraph_path, author)
        else:
            log.warning("TELEGRAPH_TOKEN не задан — страница словаря не обновляется")
    if publish or env("OPENAI_API_KEY"):
        llm: LLM = OpenAILLM(cfg.llm, state, run_id, clock, cfg.channel.tz, ledger=ledger)
    else:
        log.warning("OPENAI_API_KEY не задан: dry-run дойдёт до шага A и остановится на каждом кандидате")
        llm = NoLLM(cfg.llm, state, run_id, clock, cfg.channel.tz)
    rt = Runtime(
        cfg=cfg, state=state, board=board, tg=tg, notifier=notifier, llm=llm,
        fetcher=HttpFetcher(cfg.fetch, clock), clock=clock, run_id=run_id, out_dir=out_dir, mode=mode,
        channel_id=channel, force=bool(args.force), glossary_page=glossary_page, registry=Registry(http),
        sources_factory=lambda chosen: [build_source(s, http, clock, cfg.gate.max_age_days) for s in chosen],
    )
    state.start_run(run_id, contour, mode, clock())
    return rt


def run_contour(args: argparse.Namespace, cfg: Config) -> int:
    from .collect import run_collect
    from .digest import run_digest
    from .glossary import run_glossary
    from .leaderboards import build_adapters
    from .pin import run_pin
    from .publish import run_publish
    from .urgent import run_urgent
    from .weekly import run_weekly

    rt = build_runtime(args, cfg, args.command)
    fn = {
        "collect": run_collect, "publish": run_publish, "urgent": run_urgent, "weekly": run_weekly,
        "pin": lambda r: run_pin(r, build_adapters(cfg)), "glossary": run_glossary, "digest": run_digest,
    }[args.command]
    status_ = "ok"
    try:
        summary = fn(rt)
    except Exception as e:
        log.exception("контур %s упал", args.command)
        rt.notifier.notify(f"контур {args.command} упал: {type(e).__name__}: {str(e)[:200]}")
        summary, status_ = {"error": f"{type(e).__name__}: {e}"}, "crashed"
    try:
        summary["llm_cost_today_usd"] = round(rt.llm.state.llm_cost_on(local_date(rt.now(), rt.cfg.channel.tz)), 4)
        summary["llm_calls_this_run"] = rt.llm.state.llm_calls_in_run(rt.run_id)
    except Exception as e:  # отчёт о расходах не должен ронять завершение запуска
        log.warning("не посчитать расходы: %s", e)
    rt.state.finish_run(rt.run_id, status_, summary, rt.now())
    rt.write_out("summary.json", json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    rt.state.close()
    if rt.llm.state is not rt.state:
        rt.llm.state.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"\nрезультаты: {rt.out_dir}")
    return 0 if status_ == "ok" else 1


# --- eval ---------------------------------------------------------------------------

def run_eval(args: argparse.Namespace, cfg: Config) -> int:
    from .fixtures import Fixture, fake_llm_factory, load_fixtures, run_scenario

    out_root = Path(args.out) / f"eval-{utc_now().strftime('%Y%m%d-%H%M%S')}"
    setup_logging(out_root, args.verbose)
    # фиксированное «сейчас»: среда, 10:30 по Москве — внутри слота 10:00
    now = parse_dt("2026-10-07T07:30:00Z")
    assert now is not None
    fixtures = load_fixtures(args.fixtures, now, args.phase)
    if args.only:
        fixtures = [f for f in fixtures if f.id == args.only]

    ledger = State(args.state) if args.llm == "real" else None  # реальные вызовы тоже идут в дневной бюджет

    def real_factory(cfg_: Config, state: State, run_id: str, clock: Any, tz: str, fx: Fixture) -> Any:
        return OpenAILLM(cfg_.llm, state, run_id, clock, tz, ledger=ledger)

    factory = fake_llm_factory if args.llm == "fake" else real_factory
    failed = 0
    print(f"{'#':<4} {'ok':<4} {'сценарий':<13} описание")
    for fx in fixtures:
        res = run_scenario(fx, cfg, now, factory, workdir=out_root / fx.id)
        failed += 0 if res.ok else 1
        print(f"{fx.id:<4} {'✅' if res.ok else '❌':<3} {fx.scenario:<13} {fx.description}")
        for m in res.mismatches:
            print(f"       ↳ {m}")
    print(f"\nитого: {len(fixtures) - failed}/{len(fixtures)} совпали; подробности в {out_root}")
    return 1 if failed else 0


# --- песочница ---------------------------------------------------------------------------

def sandbox_export(args: argparse.Namespace, cfg: Config) -> int:
    from .sandbox import export_requests, manual_request

    setup_logging(None)
    if args.test:
        http = httpx.Client(timeout=cfg.fetch.timeout_s, headers={"User-Agent": cfg.fetch.user_agent})
        requests = manual_request(Registry(http), args.test)
    else:
        state = State(args.state)
        requests = export_requests(state, cfg.sandbox.max_requests_per_run, now=utc_now(),
                                   max_age_hours=cfg.sandbox.max_age_hours, max_attempts=cfg.sandbox.max_attempts)
        state.close()
    out = Path(args.out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"заявок в песочницу: {len(requests)}")
    return 0


def sandbox_apply(args: argparse.Namespace, cfg: Config) -> int:
    from .sandbox import apply_results, charge_lost_run

    setup_logging(None)
    state = State(args.state)
    if not Path(args.results).exists():
        charged = charge_lost_run(state, Path(args.requests), utc_now(), max_attempts=cfg.sandbox.max_attempts)
        state.close()
        print(f"песочница: результатов нет (job run потерян) — попытка засчитана заявке {charged or '—'}")
        return 0
    counts = apply_results(state, Path(args.results), Path(args.requests), utc_now(),
                           max_attempts=cfg.sandbox.max_attempts)
    state.close()
    print(f"песочница: запущено {counts['ok']}, не запустилось {counts['failed']}, "
          f"повторим {counts['retry']}, пропущено {counts['ignored']}")
    return 0


# --- служебные ---------------------------------------------------------------------------

def admin_report(args: argparse.Namespace) -> int:
    """Отчёт о работе (за день, за сессию) — админу в личку от бота. Разметка проверяется до отправки."""
    from .lint import parse_tg_html
    from .telegram import split_message

    text = Path(args.file).read_text(encoding="utf-8").strip()
    if errors := parse_tg_html(text).errors:
        print(f"отчёт не отправлен: разметка Telegram с ошибками: {', '.join(errors[:5])}", file=sys.stderr)
        return 2
    tg = Telegram(env("TELEGRAM_BOT_TOKEN", required=True))
    chat = env("ADMIN_CHAT_ID", required=True)
    parts = split_message(text)
    for part in parts:
        tg.send_message(chat, part, preview=False)
    print(f"отчёт отправлен в личку: {len(parts)} сообщ.")
    return 0


def notion_setup(cfg: Config) -> int:
    from .notion_board import NotionBoard

    setup_logging(None)
    board = _notion_board(cfg)
    assert isinstance(board, NotionBoard)
    created = board.setup()
    print("созданы базы: " + (", ".join(created) if created else "ничего, всё уже есть"))
    try:
        paused = board.settings().pause
    except BoardUnavailable as e:
        print(f"«Настройки» не прочитать: {e}")
        return 0
    if paused:
        print("В «Настройках» стоит флажок «Пауза» — снимите его, когда будете готовы к публикациям.")
    else:
        print("Пауза в «Настройках» снята — бот публикует по расписанию.")
    return 0


def telegraph_setup(cfg: Config) -> int:
    from .glossary_page import setup

    setup_logging(None)
    if env("TELEGRAPH_TOKEN"):
        print("TELEGRAPH_TOKEN уже задан — страница существует. Повторно не создаю.")
        return 1
    author_url = f"https://t.me/{cfg.channel.username}" if cfg.channel.username else ""
    token, path, url = setup(author_url)
    env_file = Path(".env")
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [ln for ln in lines if not ln.startswith("TELEGRAPH_TOKEN=")] + [f"TELEGRAPH_TOKEN={token}"]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("токен записан в .env (TELEGRAPH_TOKEN) — его же нужно положить в GitHub Secrets")
    print(f"path: {path}\nurl:  {url}\nВпишите их в config.yaml → glossary.telegraph_path / telegraph_url")
    return 0


def status(args: argparse.Namespace, cfg: Config) -> int:
    from .timeutil import local_date

    setup_logging(None)
    state = State(args.state)
    now = utc_now()
    today = local_date(now, cfg.channel.tz)
    info: dict[str, Any] = {
        "date": today.isoformat(),
        "regular_published_today": state.count_published(today, urgent=False),
        "urgent_published_today": state.count_published(today, urgent=True),
        "llm_cost_today_usd": round(state.llm_cost_on(today), 4),
        "published_last_7_days": [
            {"title": p.title, "rubric": p.rubric, "at": p.published_at.isoformat()}
            for p in state.published_since(today - timedelta(days=7))
        ],
        "last_runs": [dict(r) for r in state.db.execute(
            "SELECT id, status, started_at FROM runs ORDER BY started_at DESC LIMIT 8").fetchall()],
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    main()
