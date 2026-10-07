from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from vibe_stack.llm import SCHEMAS, BudgetExceeded, FakeLLM, LLMError, OpenAILLM, Usage
from vibe_stack.storage import State
from vibe_stack.telegram import Telegram, TelegramError


def fake(cfg, tmp_path, now, responses, cost=0.0, run_id="r1"):
    return FakeLLM(cfg.llm, State(tmp_path / "s.db"), run_id, lambda: now, "Europe/Moscow", responses, cost)


def test_max_calls_per_run(cfg, tmp_path, now) -> None:
    cfg.llm.max_calls_per_run = 2
    llm = fake(cfg, tmp_path, now, {("write", "a"): "text"})
    llm.text("write", "p", "a")
    llm.text("write", "p", "a")
    with pytest.raises(BudgetExceeded):
        llm.text("write", "p", "a")


def test_daily_budget(cfg, tmp_path, now) -> None:
    cfg.llm.daily_budget_usd = 1.0
    llm = fake(cfg, tmp_path, now, {("write", "a"): "text"}, cost=0.6)
    llm.text("write", "p", "a")
    llm.text("write", "p", "a")  # 1.2 > 1.0 — следующий уже нельзя
    with pytest.raises(BudgetExceeded):
        llm.text("write", "p", "a")


def test_json_validation_rejects_out_of_range(cfg, tmp_path, now) -> None:
    bad = {"id": "x", "category": "tool", "scores": {"novelty": 9, "usefulness": 1, "verifiability": 1,
                                                     "substance": 1, "audience_fit": 1},
           "hard_stops": [], "reason": "", "claims": []}
    llm = fake(cfg, tmp_path, now, {("score", "x"): bad})
    with pytest.raises(LLMError):
        llm.json("score", "p", "x")


def test_cost_formula(cfg, tmp_path, now) -> None:
    llm = OpenAILLM.__new__(OpenAILLM)
    llm.cfg = cfg.llm
    c = llm.cost("gpt-5.6-terra", Usage(input_tokens=1_000_000, cached_tokens=200_000, output_tokens=100_000))
    assert c == pytest.approx(0.8 * 2.5 + 0.2 * 0.25 + 0.1 * 15)


def test_openai_call_shape(cfg, tmp_path, now, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-000000")
    llm = OpenAILLM(cfg.llm, State(tmp_path / "s.db"), "r", lambda: now, "Europe/Moscow")
    captured = {}

    def create(**kw):
        captured.update(kw)
        payload = {"verdict": "pass", "fail_reason": "", "checks": [], "approved_claims": []}
        return SimpleNamespace(status="completed", output_text=json.dumps(payload),
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                                     input_tokens_details=SimpleNamespace(cached_tokens=0)))

    llm.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    res = llm.json("verify", "prompt", "c1")
    assert res.verdict == "pass"
    assert captured["model"] == "gpt-5.6-terra" and captured["reasoning"] == {"effort": "medium"}
    assert captured["store"] is False
    assert captured["text"]["format"]["strict"] is True
    cost = llm.state.db.execute("SELECT SUM(cost_usd) FROM llm_calls").fetchone()[0]
    assert cost == pytest.approx((1000 * 2.5 + 200 * 15) / 1_000_000)


def test_incomplete_response_counts_and_fails(cfg, tmp_path, now, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-000000")
    llm = OpenAILLM(cfg.llm, State(tmp_path / "s.db"), "r", lambda: now, "Europe/Moscow")
    llm.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        status="incomplete", output_text="", incomplete_details={"reason": "max_output_tokens"},
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, input_tokens_details=None))))
    with pytest.raises(LLMError):
        llm.text("write", "p", "c")
    assert llm.state.llm_calls_in_run("r") == 1


def test_strict_schemas_are_closed() -> None:
    for schema in SCHEMAS.values():
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


# --- Telegram ---------------------------------------------------------------------------

def tg_with(handler) -> tuple[Telegram, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Telegram("123:ABC", client=client, sleep=sleeps.append), sleeps


def test_telegram_success() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["parse_mode"] == "HTML"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    tg, _ = tg_with(handler)
    assert tg.send_message("@ch", "<b>x</b>") == 77


def test_telegram_retries_three_times_on_connection_errors() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused")

    tg, sleeps = tg_with(handler)
    with pytest.raises(TelegramError) as e:
        tg.send_message("@ch", "x")
    assert len(calls) == 4 and len(sleeps) == 3  # попытка + три повтора с паузой
    assert not e.value.uncertain


def test_send_not_repeated_when_delivery_uncertain() -> None:
    """Таймаут чтения или 5xx после отправки: пост мог выйти — повтор дал бы дубль."""
    for failure in (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
                    lambda r: httpx.Response(502, json={"ok": False, "error_code": 502, "description": "Bad"})):
        calls = []

        def handler(req: httpx.Request, failure=failure, calls=calls) -> httpx.Response:
            calls.append(1)
            return failure(req)

        tg, _ = tg_with(handler)
        with pytest.raises(TelegramError) as e:
            tg.send_message("@ch", "x")
        assert len(calls) == 1 and e.value.uncertain


def test_idempotent_methods_retry_on_5xx() -> None:
    seq = iter([httpx.Response(502, json={"ok": False, "error_code": 502, "description": "Bad"}),
                httpx.Response(200, json={"ok": True, "result": True})])
    tg, sleeps = tg_with(lambda r: next(seq))
    tg.pin_chat_message("@ch", 5)
    assert len(sleeps) == 1


def test_preview_points_to_source() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content)["link_preview_options"] == {"url": "https://src.dev/a"}
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg, _ = tg_with(handler)
    tg.send_message("@ch", "x", preview_url="https://src.dev/a")


def test_telegram_400_not_retried_and_429_respected() -> None:
    tg, _ = tg_with(lambda r: httpx.Response(400, json={"ok": False, "error_code": 400,
                                                         "description": "can't parse entities"}))
    with pytest.raises(TelegramError) as e:
        tg.send_message("@ch", "<b>")
    assert not e.value.retryable
    seq = iter([httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many",
                                          "parameters": {"retry_after": 3}}),
                httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})])
    tg2, sleeps = tg_with(lambda r: next(seq))
    assert tg2.send_message("@ch", "x") == 5 and sleeps == [3.0]


def test_telegram_errors_do_not_leak_token() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom https://api.telegram.org/bot123:ABC/sendMessage")

    tg, _ = tg_with(handler)
    with pytest.raises(TelegramError) as e:
        tg.send_message("@ch", "x")
    assert "123:ABC" not in str(e.value)


def _openai_llm(cfg, tmp_path, now, monkeypatch, create, ledger=None):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-000000")
    llm = OpenAILLM(cfg.llm, State(tmp_path / "copy.db"), "r", lambda: now, "Europe/Moscow", ledger=ledger,
                    sleep=lambda s: None)
    llm.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return llm


def _ok(**kw):
    return SimpleNamespace(status="completed", output_text="готово",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=10, input_tokens_details=None))


def test_timeout_is_retried_and_billed_conservatively(cfg, tmp_path, now, monkeypatch) -> None:
    import httpx2
    import openai

    attempts = iter([openai.APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com/v1/responses")),
                     None])

    def create(**kw):
        err = next(attempts)
        if err:
            raise err
        return _ok()

    llm = _openai_llm(cfg, tmp_path, now, monkeypatch, create)
    assert llm.text("write", "x" * 3000, "c") == "готово"
    rows = llm.state.db.execute("SELECT ok, output_tokens, cost_usd FROM llm_calls ORDER BY id").fetchall()
    assert [r["ok"] for r in rows] == [0, 1]
    assert rows[0]["output_tokens"] == cfg.llm.steps["write"].max_output_tokens  # с запасом
    assert rows[0]["cost_usd"] > 0.1


def test_client_error_not_retried(cfg, tmp_path, now, monkeypatch) -> None:
    import httpx2
    import openai

    calls = []

    def create(**kw):
        calls.append(1)
        raise openai.BadRequestError("bad", response=httpx2.Response(
            400, request=httpx2.Request("POST", "https://api.openai.com/v1/responses")), body=None)

    llm = _openai_llm(cfg, tmp_path, now, monkeypatch, create)
    with pytest.raises(LLMError):
        llm.text("write", "p", "c")
    assert len(calls) == 1


def test_dry_run_spend_goes_to_real_ledger(cfg, tmp_path, now, monkeypatch) -> None:
    real = State(tmp_path / "real.db")
    llm = _openai_llm(cfg, tmp_path, now, monkeypatch, lambda **kw: _ok(), ledger=real)
    llm.text("write", "p", "c")
    assert real.db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
    copy = State(tmp_path / "copy.db")
    assert copy.db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_daily_token_limit_blocks_call_that_could_exceed(cfg, tmp_path, now) -> None:
    from datetime import timedelta

    cfg.llm.daily_budget_usd = None
    cfg.llm.steps["write"].max_output_tokens = 1000
    cfg.llm.daily_token_limits = {"gpt-5.6-terra": 4000}
    llm = fake(cfg, tmp_path, now, {("write", "a"): "text"})
    llm.output_tokens = 1500  # каждый вызов пишет в журнал 100 (вход) + 1500 (выход)
    llm.text("write", "p" * 400, "a")
    llm.text("write", "p" * 400, "a")  # 1600 + резерв 200 + 1000 = 2800 ≤ 4000
    with pytest.raises(BudgetExceeded, match="лимит токенов"):
        llm.text("write", "p" * 400, "a")  # 3200 + 1200 > 4000 — вызов мог бы выйти за лимит
    assert llm.state.llm_tokens_since("gpt-5.6-terra", now - timedelta(hours=1)) == 3200


def test_daily_token_limit_resets_at_utc_midnight(cfg, tmp_path, now) -> None:
    from datetime import UTC, datetime, timedelta

    cfg.llm.daily_token_limits = {"gpt-5.6-terra": 3000}
    cfg.llm.steps["write"].max_output_tokens = 1000
    state = State(tmp_path / "s.db")
    t = datetime(2026, 10, 7, 23, 50, tzinfo=UTC)
    clock = {"now": t}
    llm = FakeLLM(cfg.llm, state, "r", lambda: clock["now"], "Europe/Moscow", {("write", "a"): "text"})
    llm.output_tokens = 2500
    llm.text("write", "p", "a")
    with pytest.raises(BudgetExceeded):
        llm.text("write", "p", "a")
    clock["now"] = t + timedelta(minutes=15)  # 00:05 UTC — новые сутки OpenAI
    llm.text("write", "p", "a")
