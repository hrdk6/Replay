"""LLM provider adapters: OpenAI (and compatible), Anthropic, and a deterministic simulator.

Adapters translate canonical messages to each provider's wire format via the
official SDKs, and translate responses back. They never log or return keys.

Notes recorded on each response (``ChatResponse.notes``) make silent
behaviour visible in reports, e.g. sampling parameters dropped because the
model rejects them.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from replay_api.logs import scrub
from replay_api.replay.canonical import message_text

DEFAULT_MAX_TOKENS = 4096


@dataclass
class ChatRequest:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)  # step index etc. (simulator only)


@dataclass
class ChatResponse:
    message: dict[str, Any]
    input_tokens: int
    output_tokens: int
    latency_ms: float
    stop_reason: str | None = None
    model: str | None = None
    notes: list[str] = field(default_factory=list)


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(scrub(message))
        self.retryable = retryable


class Provider(Protocol):
    name: str

    async def chat(self, req: ChatRequest) -> ChatResponse: ...


def infer_provider(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    if m.startswith("sim-"):
        return "simulator"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt-", "o1", "o3", "o4", "chatgpt", "gpt5")):
        return "openai"
    return None


# --- OpenAI ------------------------------------------------------------------------

_OPENAI_REASONING = re.compile(r"^(o\d|gpt-5)")


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append(
                {"role": "tool", "tool_call_id": m.get("tool_call_id") or "", "content": str(m.get("content") or "")}
            )
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content")}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}))},
                    }
                    for c in m["tool_calls"]
                ]
            out.append(msg)
        else:
            out.append({"role": role or "user", "content": m.get("content") or ""})
    return out


class OpenAIProvider:
    name = "openai"

    def __init__(
        self, api_key: str, base_url: str | None, timeout: float, max_retries: int, compatible: bool = False
    ) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)
        self._compatible = compatible
        if compatible:
            self.name = "openai_compatible"

    async def chat(self, req: ChatRequest) -> ChatResponse:
        import openai

        notes: list[str] = []
        kwargs: dict[str, Any] = {"model": req.model, "messages": to_openai_messages(req.messages)}
        if req.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
                for t in req.tools
            ]
        p = dict(req.params)
        reasoning = not self._compatible and bool(_OPENAI_REASONING.match(req.model.lower()))
        for k in ("temperature", "top_p"):
            if k in p and p[k] is not None:
                if reasoning:
                    notes.append(f"dropped {k}: not supported by {req.model}")
                else:
                    kwargs[k] = p[k]
        if p.get("seed") is not None:
            kwargs["seed"] = p["seed"]
        if p.get("stop"):
            kwargs["stop"] = p["stop"]
        max_tokens = int(p.get("max_tokens") or DEFAULT_MAX_TOKENS)
        kwargs["max_tokens" if self._compatible else "max_completion_tokens"] = max_tokens
        if p.get("effort") and reasoning:
            kwargs["reasoning_effort"] = p["effort"]
        start = time.perf_counter()
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except (
            openai.RateLimitError,
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.InternalServerError,
        ) as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
        except openai.APIError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        latency = (time.perf_counter() - start) * 1000
        data = resp.model_dump()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""), "arguments": args})
        message: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
        if calls:
            message["tool_calls"] = calls
        usage = data.get("usage") or {}
        return ChatResponse(
            message,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            latency,
            choice.get("finish_reason"),
            data.get("model"),
            notes,
        )


# --- Anthropic -------------------------------------------------------------------

# Older Claude models still accept sampling parameters; newer ones reject them (400).
_ANTHROPIC_SAMPLING_OK = re.compile(
    r"^claude-(3|instant|2|sonnet-4-6|opus-4-6|haiku-4-5|sonnet-4-5|opus-4-5|opus-4-1|opus-4-0|sonnet-4-0|opus-4-2|sonnet-4-2)"
)


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(str(m["content"]))
            continue
        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": str(m.get("content") or ""),
                }
            )
            continue
        flush_results()
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("arguments") or {}})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            text = str(m.get("content") or "")
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append({"type": "text", "text": text})
            else:
                out.append({"role": "user", "content": text})
    flush_results()
    return ("\n\n".join(system_parts) or None), out


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, timeout: float, max_retries: int) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)

    async def chat(self, req: ChatRequest) -> ChatResponse:
        import anthropic

        notes: list[str] = []
        system, messages = to_anthropic(req.messages)
        p = dict(req.params)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
            "max_tokens": int(p.get("max_tokens") or DEFAULT_MAX_TOKENS),
        }
        if system:
            kwargs["system"] = system
        if req.tools:
            kwargs["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                }
                for t in req.tools
            ]
        sampling_ok = bool(_ANTHROPIC_SAMPLING_OK.match(req.model.lower()))
        for k in ("temperature", "top_p"):
            if p.get(k) is not None:
                if sampling_ok:
                    kwargs[k] = p[k]
                else:
                    notes.append(f"dropped {k}: {req.model} does not accept sampling parameters")
        if p.get("seed") is not None:
            notes.append("dropped seed: not supported by the Anthropic API")
        if p.get("stop"):
            kwargs["stop_sequences"] = p["stop"] if isinstance(p["stop"], list) else [p["stop"]]
        extra: dict[str, Any] = {}
        if p.get("effort"):
            extra["output_config"] = {"effort": p["effort"]}
        # Deliberately NO server-side model fallback: a refusal must be recorded as
        # this model's output, not silently answered by a different model.
        start = time.perf_counter()
        try:
            resp = await self._client.messages.create(**kwargs, extra_body=extra or None)
        except (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.InternalServerError,
        ) as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
        except anthropic.APIError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        latency = (time.perf_counter() - start) * 1000
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append({"id": block.id, "name": block.name, "arguments": dict(block.input or {})})
        message: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
        if calls:
            message["tool_calls"] = calls
        if resp.stop_reason == "refusal":
            notes.append("model refused (stop_reason=refusal)")
        return ChatResponse(
            message,
            int(resp.usage.input_tokens or 0),
            int(resp.usage.output_tokens or 0),
            latency,
            resp.stop_reason,
            resp.model,
            notes,
        )


# --- Simulator ----------------------------------------------------------------------


def _parse_sim_model(model: str) -> tuple[str, dict[str, float]]:
    base, _, rest = model.partition(":")
    params: dict[str, float] = {}
    for part in rest.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                params[k.strip()] = float(v)
            except ValueError:
                continue
    return base, params


def _rng(*parts: Any) -> random.Random:
    seed = int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16)
    return random.Random(seed)


class SimulatorProvider:
    """Deterministic stand-in for an LLM, driven by the recording.

    * ``sim-replay`` reproduces the recorded outputs exactly (a perfect agent).
    * ``sim-perturb:q=0.8,noise=0.1,div=0.05,verbose=0,seed=1`` degrades
      answers with probability ``1-q``, perturbs tool arguments with
      probability ``noise`` (exercises fuzzy matching) and calls an
      unrecorded tool with probability ``div`` (exercises divergence).

    It exists for tests, demos and pipeline checks. It says nothing about any
    real model's quality.
    """

    name = "simulator"

    def __init__(self, recorded_outputs: list[dict[str, Any] | None]) -> None:
        self._outputs = recorded_outputs

    async def chat(self, req: ChatRequest) -> ChatResponse:
        base, params = _parse_sim_model(req.model)
        step = int(req.meta.get("step", 0))
        mode = req.meta.get("mode", "full_agent")
        item = req.meta.get("item_key", "")
        rep = req.meta.get("repeat", 0)
        if mode == "single_turn" or step >= len(self._outputs):
            recorded = self._outputs[-1] if self._outputs else None
            if mode != "single_turn" and recorded and recorded.get("tool_calls"):
                recorded = {"role": "assistant", "content": "Done."}
        else:
            recorded = self._outputs[step]
        msg: dict[str, Any] = json.loads(json.dumps(recorded)) if recorded else {"role": "assistant", "content": ""}
        msg["role"] = "assistant"
        notes: list[str] = []
        if base == "sim-perturb":
            rng = _rng(params.get("seed", 0), item, rep, step)
            calls: list[dict[str, Any]] = msg.get("tool_calls") or []
            for c in calls:
                if rng.random() < params.get("div", 0.0):
                    c["name"] = f"{c['name']}_v2"
                elif rng.random() < params.get("noise", 0.0):
                    c["arguments"] = _perturb_args(c.get("arguments") or {}, rng)
            if not calls and msg.get("content"):
                if rng.random() > params.get("q", 1.0):
                    msg["content"] = _degrade(str(msg["content"]), rng)
                if params.get("verbose", 0) > 0:
                    msg["content"] = (
                        str(msg["content"]) + " " + "Let me know if you need anything else." * int(params["verbose"])
                    )
        elif base != "sim-replay":
            raise ProviderError(f"unknown simulator model {req.model!r}")
        in_chars = sum(len(message_text(m)) for m in req.messages)
        out_chars = len(message_text(msg))
        latency = 150.0 + out_chars * 0.8 + _rng(item, rep, step, base).random() * 50
        return ChatResponse(msg, max(1, in_chars // 4), max(1, out_chars // 4), latency, "end_turn", req.model, notes)


def _perturb_args(args: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    out = dict(args)
    for k, v in out.items():
        if isinstance(v, str) and v:
            out[k] = v + " please" if rng.random() < 0.5 else v.upper()
            return out
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = v + 1
            return out
    return out


def _degrade(text: str, rng: random.Random) -> str:
    choice = rng.random()
    if choice < 0.4:
        return "I'm sorry, I don't have enough information to answer that."
    if choice < 0.8:
        first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
        return first if first != text else text[: max(10, len(text) // 3)]
    return re.sub(r"\d", "0", text)
