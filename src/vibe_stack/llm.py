"""Вызовы модели (OpenAI Responses API) с защитой от расходов.

Каждый вызов — отдельный запрос без истории (previous_response_id не используется), поэтому
проверка B всегда идёт в чистом контексте. Ответы-JSON идут через Structured Outputs и
дополнительно валидируются pydantic.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .config import LLMConfig, env
from .models import HARD_STOPS, GlossaryResult, ScoreResult, TriageResult, VerifyResult
from .storage import State
from .timeutil import Clock, local_date

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    """Лимит вызовов за запуск или дневной бюджет исчерпан — запуск останавливается."""


class LLMError(RuntimeError):
    pass


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


def _int_enum(top: int) -> dict[str, Any]:
    return {"type": "integer", "enum": list(range(top + 1))}


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}
_HARD_STOPS = {"type": "array", "items": {"type": "string", "enum": list(HARD_STOPS)}}

SCHEMAS: dict[str, dict[str, Any]] = {
    "score": _obj({
        "id": _STR,
        "category": {"type": "string", "enum": [
            "tool", "skill_mcp", "trick", "case", "book_video", "benchmark", "analysis_candidate", "none"]},
        "scores": _obj({
            "novelty": _int_enum(3), "usefulness": _int_enum(5), "verifiability": _int_enum(3),
            "substance": _int_enum(2), "audience_fit": _int_enum(2),
        }),
        "hard_stops": _HARD_STOPS,
        "reason": _STR,
        "claims": _STR_LIST,
    }),
    "verify": _obj({
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "fail_reason": _STR,
        "checks": {"type": "array", "items": _obj({
            "claim": _STR,
            "status": {"type": "string", "enum": ["supported", "not_supported", "unclear"]},
            "evidence": _STR,
        })},
        "approved_claims": _STR_LIST,
    }),
    "triage": _obj({
        "id": _STR,
        "event": {"type": "string", "enum": [
            "new_model", "major_release", "pricing_or_limits_change", "vulnerability", "none"]},
        "hard_stops": _HARD_STOPS,
        "reason": _STR,
        "claims": _STR_LIST,
    }),
    "glossary": _obj({
        "id": _STR,
        "term": _STR,
        "has_definition": {"type": "boolean"},
        "hard_stops": _HARD_STOPS,
        "claims": _STR_LIST,
        "example": _STR,
        "not_to_confuse": _STR,
    }),
}
RESULT_MODELS: dict[str, type[BaseModel]] = {"score": ScoreResult, "verify": VerifyResult, "triage": TriageResult,
                                             "glossary": GlossaryResult}


class Usage(BaseModel):
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0


class LLM:
    """Базовый класс: бюджет, учёт, повторы, разбор JSON. Транспорт — в _call.

    ledger — журнал расходов. В dry-run это настоящее состояние, а не копия: деньги тратятся по-настоящему,
    и дневной бюджет должен их видеть.
    """

    def __init__(self, cfg: LLMConfig, state: State, run_id: str, clock: Clock, tz: str,
                 ledger: State | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.state = ledger or state
        self.run_id = run_id
        self.clock = clock
        self.tz = tz
        self.sleep = sleep

    # --- транспорт ---------------------------------------------------------------------------
    def _call(
        self, step: str, prompt: str, schema: dict[str, Any] | None, ctx_id: str
    ) -> tuple[str, Usage, bool]:
        """(текст, расход, завершён ли ответ). Для незавершённого ответа текст — причина."""
        raise NotImplementedError

    def _classify(self, exc: Exception, step: str, prompt: str) -> tuple[bool, Usage]:
        """(повторять ли, во что оценить неудачную попытку). По умолчанию — не повторять, $0."""
        return False, Usage()

    # --- бюджет ---------------------------------------------------------------------------
    def cost(self, model: str, u: Usage) -> float:
        price = self.cfg.prices_per_1m.get(model)
        if price is None:
            raise LLMError(f"для модели {model} нет цены в llm.prices_per_1m — бюджет не посчитать")
        cached_price = price.cached_input if price.cached_input is not None else price.input
        fresh = max(u.input_tokens - u.cached_tokens, 0)
        return (fresh * price.input + u.cached_tokens * cached_price + u.output_tokens * price.output) / 1_000_000

    def check_budget(self, model: str | None = None, reserve_tokens: int = 0) -> None:
        """reserve_tokens — сколько токенов может занять предстоящий вызов (оценка входа + максимум выхода)."""
        calls = self.state.llm_calls_in_run(self.run_id)
        if calls >= self.cfg.max_calls_per_run:
            raise BudgetExceeded(f"достигнут MAX_LLM_CALLS_PER_RUN={self.cfg.max_calls_per_run}")
        if self.cfg.daily_budget_usd is not None:
            spent = self.state.llm_cost_on(local_date(self.clock(), self.tz))
            if spent >= self.cfg.daily_budget_usd:
                raise BudgetExceeded(f"дневной бюджет ${self.cfg.daily_budget_usd:.2f} исчерпан (${spent:.2f})")
        limit = self.cfg.daily_token_limits.get(model or "")
        if limit is not None:
            day_start = self.clock().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            used = self.state.llm_tokens_since(model or "", day_start)
            if used + reserve_tokens > limit:
                raise BudgetExceeded(f"дневной лимит токенов {model}: {limit:,} — использовано {used:,}, "
                                     f"вызов может занять ещё до {reserve_tokens:,}")

    def reserve_tokens(self, step: str, prompt: str) -> int:
        """Верхняя оценка токенов вызова: вход ≈ байты UTF-8 / 2 (с запасом) + максимум выхода шага."""
        return len(prompt.encode("utf-8")) // 2 + self.cfg.steps[step].max_output_tokens

    def _invoke(self, step: str, prompt: str, schema: dict[str, Any] | None, ctx_id: str) -> str:
        step_cfg = self.cfg.steps.get(step)
        if step_cfg is None:
            raise LLMError(f"нет настроек шага llm.steps.{step}")
        if step_cfg.model not in self.cfg.prices_per_1m:
            raise LLMError(f"для модели {step_cfg.model} нет цены в llm.prices_per_1m — бюджет не посчитать")
        for attempt in range(self.cfg.max_attempts):
            # каждая попытка — отдельный вызов в лимите и бюджете
            self.check_budget(step_cfg.model, self.reserve_tokens(step, prompt))
            now = self.clock()
            day = local_date(now, self.tz)
            try:
                text, usage, completed = self._call(step, prompt, schema, ctx_id)
            except Exception as e:
                retry, estimate = self._classify(e, step, prompt)
                self.state.record_llm_call(run_id=self.run_id, step=step, model=step_cfg.model, now=now, day=day,
                                           input_tokens=estimate.input_tokens, cached_tokens=0,
                                           output_tokens=estimate.output_tokens,
                                           cost_usd=self.cost(step_cfg.model, estimate), ok=False)
                if retry and attempt + 1 < self.cfg.max_attempts:
                    log.info("llm %s %s: %s, повтор", step, ctx_id, type(e).__name__)
                    self.sleep(3.0 * (attempt + 1))
                    continue
                raise LLMError(f"{step}: {type(e).__name__}: {str(e)[:300]}") from e
            cost = self.cost(step_cfg.model, usage)
            self.state.record_llm_call(run_id=self.run_id, step=step, model=step_cfg.model, now=now, day=day,
                                       input_tokens=usage.input_tokens, cached_tokens=usage.cached_tokens,
                                       output_tokens=usage.output_tokens, cost_usd=cost, ok=completed)
            log.debug("llm %s %s: in=%d out=%d $%.4f", step, ctx_id, usage.input_tokens, usage.output_tokens, cost)
            if not completed:
                raise LLMError(f"{step}: ответ не завершён ({text})")
            return text
        raise AssertionError("unreachable")

    # --- публичные методы ---------------------------------------------------------------------------
    def json(self, step: str, prompt: str, ctx_id: str) -> Any:
        text = self._invoke(step, prompt, SCHEMAS[step], ctx_id)
        try:
            data = json.loads(_strip_fences(text))
            return RESULT_MODELS[step].model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            raise LLMError(f"{step}: ответ не прошёл валидацию: {str(e)[:300]}") from e

    def text(self, step: str, prompt: str, ctx_id: str) -> str:
        return _strip_fences(self._invoke(step, prompt, None, ctx_id)).strip()


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class OpenAILLM(LLM):
    def __init__(self, cfg: LLMConfig, state: State, run_id: str, clock: Clock, tz: str,
                 ledger: State | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(cfg, state, run_id, clock, tz, ledger, sleep)
        from openai import OpenAI

        self.client = OpenAI(
            api_key=env("OPENAI_API_KEY", required=True),
            base_url=cfg.base_url,
            timeout=cfg.request_timeout_s,
            max_retries=0,  # повторы делает _invoke и учитывает каждую попытку
        )

    def _classify(self, exc: Exception, step: str, prompt: str) -> tuple[bool, Usage]:
        import openai

        est_in = len(prompt) // 3  # грубая оценка токенов: смесь русского и английского
        if isinstance(exc, openai.APITimeoutError):
            # запрос мог дойти и быть оплачен целиком — считаем с запасом
            return True, Usage(input_tokens=est_in, output_tokens=self.cfg.steps[step].max_output_tokens)
        if isinstance(exc, openai.APIConnectionError | openai.RateLimitError):
            return True, Usage()
        if isinstance(exc, openai.InternalServerError):
            return True, Usage(input_tokens=est_in)
        return False, Usage()

    def _call(
        self, step: str, prompt: str, schema: dict[str, Any] | None, ctx_id: str
    ) -> tuple[str, Usage, bool]:
        sc = self.cfg.steps[step]
        if sc.model not in self.cfg.prices_per_1m:
            raise LLMError(f"для модели {sc.model} нет цены в llm.prices_per_1m — бюджет не посчитать")
        kwargs: dict[str, Any] = {
            "model": sc.model,
            "input": prompt,
            "reasoning": {"effort": sc.effort},
            "max_output_tokens": sc.max_output_tokens,
            "store": False,
        }
        if schema is not None:
            kwargs["text"] = {"format": {"type": "json_schema", "name": f"vibe_stack_{step}", "schema": schema,
                                         "strict": True}}
        resp = self.client.responses.create(**kwargs)
        u = resp.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            cached_tokens=getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
        )
        if resp.status != "completed":
            return f"status={resp.status} {getattr(resp, 'incomplete_details', None)}", usage, False
        return resp.output_text, usage, True


class NoLLM(LLM):
    """Dry-run без OPENAI_API_KEY: всё до вызова модели работает, сам вызов — понятная ошибка."""

    def _invoke(self, step: str, prompt: str, schema: dict[str, Any] | None, ctx_id: str) -> str:
        raise LLMError("OPENAI_API_KEY не задан — dry-run остановлен перед вызовом модели")


class FakeLLM(LLM):
    """Ответы из фикстур: {(step, ctx_id): dict | str | callable(prompt) -> ...}."""

    def __init__(self, cfg: LLMConfig, state: State, run_id: str, clock: Clock, tz: str,
                 responses: dict[tuple[str, str], Any], cost_per_call: float = 0.0,
                 ledger: State | None = None) -> None:
        super().__init__(cfg, state, run_id, clock, tz, ledger)
        self.responses = responses
        self.cost_per_call = cost_per_call
        self.output_tokens = 0  # сколько «выхода» записывать в журнал за вызов (для тестов лимита токенов)
        self.calls: list[tuple[str, str, str]] = []

    def cost(self, model: str, u: Usage) -> float:
        return self.cost_per_call

    def _invoke(self, step: str, prompt: str, schema: dict[str, Any] | None, ctx_id: str) -> str:
        step_cfg = self.cfg.steps.get(step)
        model = step_cfg.model if step_cfg else "fake"
        self.check_budget(model, self.reserve_tokens(step, prompt) if step_cfg else 0)
        now = self.clock()
        self.calls.append((step, ctx_id, prompt))
        key = (step, ctx_id)
        if key not in self.responses:
            self.state.record_llm_call(run_id=self.run_id, step=step, model=model, now=now,
                                       day=local_date(now, self.tz), input_tokens=0, cached_tokens=0,
                                       output_tokens=0, cost_usd=0.0, ok=False)
            raise LLMError(f"нет фейкового ответа для {key}")
        payload = self.responses[key]
        if callable(payload):
            payload = payload(prompt)
        self.state.record_llm_call(run_id=self.run_id, step=step, model=model, now=now,
                                   day=local_date(now, self.tz), input_tokens=len(prompt) // 4, cached_tokens=0,
                                   output_tokens=self.output_tokens, cost_usd=self.cost_per_call, ok=True)
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
