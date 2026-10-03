"""Replay engine: re-run a recording with an arm configuration.

LLM calls are re-executed; tool and retrieval results always come from the
recording, so no user code runs and no outside system is touched.

Modes
-----
* ``single_turn``: replace only the final LLM call. Its recorded input
  (including recorded tool results) is replayed with the arm's overrides.
  If the arm asks for a tool where the recording produced a final answer,
  the run is DIVERGED (``unexpected_tool_call``).
* ``full_agent``: start from the first LLM call's input and run the loop.
  Each requested tool call is matched against the recording (see
  ``matching``); no match -> DIVERGED at that step with the partial result.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from replay_api.replay.canonical import LLMCall
from replay_api.replay.matching import ToolMatcher
from replay_api.replay.providers import ChatRequest, ChatResponse, Provider, ProviderError
from replay_api.replay.recording import Recording
from replay_api.services.pricing import Price, cost_usd, estimate_tokens_from_text, price_for

Mode = Literal["single_turn", "full_agent"]
RunStatus = Literal["completed", "diverged", "failed", "skipped"]


class ReplayConfigError(Exception):
    """The arm cannot be applied to this recording (reported as a failed run)."""


class BudgetExhausted(Exception):
    pass


class BudgetGuard(Protocol):
    async def reserve(self, amount: Decimal) -> bool: ...
    async def settle(self, reserved: Decimal, actual: Decimal) -> None: ...


class UnlimitedBudget:
    async def reserve(self, amount: Decimal) -> bool:
        return True

    async def settle(self, reserved: Decimal, actual: Decimal) -> None:
        return None


@dataclass
class ArmConfig:
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    prompt_template: dict[str, Any] | None = None  # {"template": str, "role": "system"|"user"}
    params: dict[str, Any] = field(default_factory=dict)
    retrieval_top_k: int | None = None
    pricing: dict[str, Any] | None = None
    provider_key_id: str | None = None

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> ArmConfig:
        cfg = cfg or {}
        retrieval = cfg.get("retrieval") or {}
        return cls(
            provider=cfg.get("provider"),
            model=cfg.get("model"),
            system_prompt=cfg.get("system_prompt"),
            prompt_template=cfg.get("prompt_template"),
            params=dict(cfg.get("params") or {}),
            retrieval_top_k=retrieval.get("top_k"),
            pricing=cfg.get("pricing"),
            provider_key_id=cfg.get("provider_key_id"),
        )


@dataclass
class ReplayOptions:
    mode: Mode = "single_turn"
    fuzzy_threshold: float = 0.85
    allow_reuse: bool = True
    typed_matching: bool = True
    max_steps: int | None = None
    hard_max_steps: int = 25


@dataclass
class RunResult:
    status: RunStatus
    output: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    divergence: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: float = 0.0
    error: str | None = None
    notes: list[str] = field(default_factory=list)


ProviderFactory = Callable[[str, str | None], Awaitable[Provider]]

_TEMPLATE_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


def render_template(template: str, variables: dict[str, Any]) -> str:
    missing: list[str] = []

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in variables:
            missing.append(key)
            return m.group(0)
        v = variables[key]
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    out = _TEMPLATE_VAR.sub(sub, template)
    if missing:
        raise ReplayConfigError(f"prompt template variables not recorded: {', '.join(sorted(set(missing)))}")
    return out


def apply_overrides(
    call: LLMCall, arm: ArmConfig, recording: Recording
) -> tuple[list[dict[str, Any]], str, str | None, dict[str, Any]]:
    messages = json.loads(json.dumps(call.messages))
    if arm.prompt_template:
        if not recording.prompt_variables:
            raise ReplayConfigError(
                "candidate uses prompt_template but the trace has no recorded prompt variables "
                "(record them as the 'replay.prompt.variables' span attribute)"
            )
        role = arm.prompt_template.get("role", "user")
        rendered = render_template(str(arm.prompt_template.get("template", "")), recording.prompt_variables)
        if role == "system":
            _set_system(messages, rendered)
        else:
            idx = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
            if idx is None:
                messages.append({"role": "user", "content": rendered})
            else:
                messages[idx]["content"] = rendered
    if arm.system_prompt is not None:
        _set_system(messages, arm.system_prompt)
    params = dict(call.params)
    for k, v in arm.params.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = v
    model = arm.model or call.model
    if not model:
        raise ReplayConfigError("no model: the recording has no model and the arm sets none")
    provider = arm.provider or (call.provider if not arm.model else None)
    return messages, model, provider, params


def _set_system(messages: list[dict[str, Any]], text: str) -> None:
    for m in messages:
        if m.get("role") == "system":
            m["content"] = text
            return
    messages.insert(0, {"role": "system", "content": text})


def _estimate_call_cost(price: Price, req: ChatRequest) -> Decimal:
    chars = sum(len(json.dumps(m, default=str)) for m in req.messages) + len(json.dumps(req.tools))
    max_out = int(req.params.get("max_tokens") or 2048)
    return cost_usd(price, estimate_tokens_from_text(chars), max_out)


def _serialize_result(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


async def _call(
    provider: Provider, req: ChatRequest, price: Price, budget: BudgetGuard, out: RunResult
) -> ChatResponse:
    reserve = _estimate_call_cost(price, req)
    if not await budget.reserve(reserve):
        raise BudgetExhausted
    try:
        resp = await provider.chat(req)
    except BaseException:
        await budget.settle(reserve, Decimal(0))
        raise
    actual = cost_usd(price, resp.input_tokens, resp.output_tokens)
    await budget.settle(reserve, actual)
    out.input_tokens += resp.input_tokens
    out.output_tokens += resp.output_tokens
    out.cost_usd += actual
    out.latency_ms += resp.latency_ms
    for n in resp.notes:
        if n not in out.notes:
            out.notes.append(n)
    out.steps.append(
        {
            "type": "llm",
            "model": resp.model or req.model,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "latency_ms": round(resp.latency_ms, 1),
            "stop_reason": resp.stop_reason,
            "tool_calls": [
                {"name": c["name"], "arguments": c.get("arguments")} for c in resp.message.get("tool_calls") or []
            ],
            "text": (resp.message.get("content") or "")[:2000],
        }
    )
    return resp


def _final_output(msg: dict[str, Any] | None, stop_reason: str | None = None) -> dict[str, Any]:
    msg = msg or {"role": "assistant", "content": None}
    return {"text": msg.get("content") or "", "message": msg, "stop_reason": stop_reason}


async def run_recorded(recording: Recording) -> RunResult:
    """Baseline taken straight from the recording (no LLM call)."""
    if recording.final_output is None:
        return RunResult("failed", error="recording has no final output")
    return RunResult(
        "completed",
        output=_final_output(recording.final_output, "recorded"),
        latency_ms=float(recording.recorded_latency_ms or 0.0),
        cost_usd=Decimal(str(recording.recorded_cost_usd or 0)),
        notes=["baseline taken from the recording (not re-run)"],
    )


async def run_replay(
    recording: Recording,
    arm: ArmConfig,
    options: ReplayOptions,
    get_provider: ProviderFactory,
    budget: BudgetGuard,
    meta: dict[str, Any] | None = None,
) -> RunResult:
    out = RunResult("failed")
    meta = dict(meta or {})
    if not recording.replayable:
        out.error = "recording has no replayable LLM call"
        return out
    try:
        if options.mode == "single_turn":
            return await _single_turn(recording, arm, options, get_provider, budget, meta, out)
        return await _full_agent(recording, arm, options, get_provider, budget, meta, out)
    except BudgetExhausted:
        out.status = "skipped"
        out.error = "budget exhausted"
        return out
    except ReplayConfigError as exc:
        out.status = "failed"
        out.error = str(exc)
        return out
    except ProviderError as exc:
        out.status = "failed"
        out.error = f"provider error: {exc}"
        return out


async def _single_turn(
    recording: Recording,
    arm: ArmConfig,
    options: ReplayOptions,
    get_provider: ProviderFactory,
    budget: BudgetGuard,
    meta: dict[str, Any],
    out: RunResult,
) -> RunResult:
    step = recording.llm_steps[-1]
    messages, model, provider_name, params = apply_overrides(step, arm, recording)
    if arm.retrieval_top_k:
        out.notes.append(
            "retrieval.top_k has no effect in single_turn mode (tool results are fixed in the recorded prompt)"
        )
    provider = await get_provider(model, provider_name)
    price = price_for(model, arm.pricing)
    if not price.known:
        out.notes.append(f"no price for {model}; cost uses a conservative default")
    req = ChatRequest(
        model, list(messages), step.tools, params, {**meta, "step": len(recording.llm_steps) - 1, "mode": "single_turn"}
    )
    resp = await _call(provider, req, price, budget, out)
    out.output = _final_output(resp.message, resp.stop_reason)
    recorded_called_tools = bool(step.output and step.output.get("tool_calls"))
    if resp.message.get("tool_calls") and not recorded_called_tools:
        out.status = "diverged"
        first = resp.message["tool_calls"][0]
        out.divergence = {
            "step": 0,
            "reason": "unexpected_tool_call",
            "detail": "the recording ended with an answer here, but this arm requested a tool",
            "requested": {"name": first["name"], "arguments": first.get("arguments")},
        }
        return out
    out.status = "completed"
    return out


async def _full_agent(
    recording: Recording,
    arm: ArmConfig,
    options: ReplayOptions,
    get_provider: ProviderFactory,
    budget: BudgetGuard,
    meta: dict[str, Any],
    out: RunResult,
) -> RunResult:
    first = recording.llm_steps[0]
    messages, model, provider_name, params = apply_overrides(first, arm, recording)
    provider = await get_provider(model, provider_name)
    price = price_for(model, arm.pricing)
    if not price.known:
        out.notes.append(f"no price for {model}; cost uses a conservative default")
    tools: dict[str, dict[str, Any]] = {}
    for s in recording.llm_steps:
        for t in s.tools:
            tools.setdefault(t["name"], t)
    matcher = ToolMatcher(
        recording.tool_events,
        options.fuzzy_threshold,
        options.allow_reuse,
        typed=options.typed_matching,
        tool_schemas=list(tools.values()),
    )
    retrieval_names = {e.name for e in recording.tool_events if e.kind == "retrieval"}
    max_steps = options.max_steps or min(options.hard_max_steps, max(4, 2 * len(recording.llm_steps) + 2))
    last_msg: dict[str, Any] | None = None
    for step_idx in range(max_steps):
        req = ChatRequest(
            model, list(messages), list(tools.values()), params, {**meta, "step": step_idx, "mode": "full_agent"}
        )
        resp = await _call(provider, req, price, budget, out)
        last_msg = resp.message
        messages.append(resp.message)
        calls = resp.message.get("tool_calls") or []
        if not calls:
            out.status = "completed"
            out.output = _final_output(resp.message, resp.stop_reason)
            return out
        for call in calls:
            m = matcher.match(call["name"], call.get("arguments") or {})
            out.steps.append(
                {
                    "type": "tool",
                    "name": call["name"],
                    "arguments": call.get("arguments"),
                    "match": m.kind,
                    "score": round(m.score, 4),
                    "reused": m.reused,
                    "recorded_index": m.event.index if m.event else None,
                }
            )
            if m.event is None:
                out.status = "diverged"
                out.divergence = {
                    "step": step_idx,
                    "reason": "no_matching_recording",
                    "detail": (
                        f"no recorded result for {call['name']} with these arguments "
                        f"(best similarity {m.best_score:.2f}, threshold {options.fuzzy_threshold:.2f})"
                    ),
                    "requested": {"name": call["name"], "arguments": call.get("arguments")},
                    "best_similarity": round(m.best_score, 4),
                }
                out.output = _final_output(last_msg, "diverged")
                return out
            result = m.event.result
            if (
                arm.retrieval_top_k
                and (m.event.kind == "retrieval" or m.event.name in retrieval_names)
                and isinstance(result, list)
            ):
                if arm.retrieval_top_k > len(result):
                    note = f"retrieval top_k={arm.retrieval_top_k} exceeds recorded results ({len(result)})"
                    if note not in out.notes:
                        out.notes.append(note)
                result = result[: arm.retrieval_top_k]
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": _serialize_result(result)})
    out.status = "diverged"
    out.divergence = {
        "step": max_steps,
        "reason": "max_steps",
        "detail": f"no final answer within {max_steps} LLM steps",
    }
    out.output = _final_output(last_msg, "max_steps")
    return out
