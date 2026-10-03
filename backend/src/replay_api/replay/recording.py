"""Turn a stored trace into a self-contained, replayable recording."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from replay_api.replay.canonical import LLMCall, extract_llm_call, parse_json_maybe
from replay_api.replay.matching import ToolEvent, args_hash

RECORDING_VERSION = 1


@dataclass
class Recording:
    trace_external_id: str
    llm_steps: list[LLMCall]
    tool_events: list[ToolEvent]
    final_output: dict[str, Any] | None
    prompt_variables: dict[str, Any] | None = None
    input_preview: str | None = None
    recorded_latency_ms: float | None = None
    recorded_cost_usd: float | None = None
    multimodal: bool = False
    notes: list[str] = field(default_factory=list)
    version: int = RECORDING_VERSION

    @property
    def replayable(self) -> bool:
        return bool(self.llm_steps) and bool(self.llm_steps[0].messages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "trace_external_id": self.trace_external_id,
            "llm_steps": [s.to_dict() for s in self.llm_steps],
            "tool_events": [e.to_dict() for e in self.tool_events],
            "final_output": self.final_output,
            "prompt_variables": self.prompt_variables,
            "input_preview": self.input_preview,
            "recorded_latency_ms": self.recorded_latency_ms,
            "recorded_cost_usd": self.recorded_cost_usd,
            "multimodal": self.multimodal,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Recording:
        return cls(
            trace_external_id=d.get("trace_external_id", ""),
            llm_steps=[LLMCall.from_dict(s) for s in d.get("llm_steps", [])],
            tool_events=[ToolEvent.from_dict(e) for e in d.get("tool_events", [])],
            final_output=d.get("final_output"),
            prompt_variables=d.get("prompt_variables"),
            input_preview=d.get("input_preview"),
            recorded_latency_ms=d.get("recorded_latency_ms"),
            recorded_cost_usd=d.get("recorded_cost_usd"),
            multimodal=bool(d.get("multimodal", False)),
            notes=list(d.get("notes", [])),
            version=int(d.get("version", RECORDING_VERSION)),
        )


def _tool_from_span(index: int, span: dict[str, Any]) -> ToolEvent:
    attrs = span.get("attributes") or {}
    data = parse_json_maybe(span.get("input"))
    name = None
    args: Any = data
    call_id = attrs.get("gen_ai.tool.call.id")
    if isinstance(data, dict) and ("arguments" in data or "name" in data):
        name = data.get("name")
        args = parse_json_maybe(data.get("arguments", {}))
        call_id = data.get("call_id") or call_id
    name = name or attrs.get("gen_ai.tool.name") or span.get("name") or "tool"
    if args is None:
        args = {}
    elif not isinstance(args, dict):
        args = {"input": args}
    return ToolEvent(
        index=index,
        kind="retrieval" if span.get("kind") == "retrieval" else "tool",
        name=str(name),
        arguments=args,
        result=parse_json_maybe(span.get("output")),
        call_id=str(call_id) if call_id else None,
        span_id=span.get("span_id"),
        source="span",
    )


def _events_from_messages(steps: list[LLMCall], start_index: int, known: list[ToolEvent]) -> list[ToolEvent]:
    """Recover tool calls/results that only appear inside LLM message histories."""
    known_ids = {e.call_id for e in known if e.call_id}
    known_keys = {(args_hash(e.name, e.arguments), json.dumps(e.result, sort_keys=True, default=str)) for e in known}
    out: list[ToolEvent] = []
    idx = start_index
    histories = [s.messages for s in steps] + [[s.output] for s in steps if s.output]
    for messages in histories:
        calls: dict[str, tuple[str, Any]] = {}
        for m in messages:
            for c in m.get("tool_calls") or []:
                calls[c["id"]] = (c["name"], c.get("arguments", {}))
        for m in messages:
            if m.get("role") != "tool":
                continue
            cid = m.get("tool_call_id") or ""
            if cid in known_ids or cid not in calls:
                continue
            name, args = calls[cid]
            result = parse_json_maybe(m.get("content"))
            key = (args_hash(name, args), json.dumps(result, sort_keys=True, default=str))
            if key in known_keys:
                known_ids.add(cid)
                continue
            out.append(ToolEvent(idx, "tool", name, args, result, cid, None, "messages"))
            known_ids.add(cid)
            known_keys.add(key)
            idx += 1
    return out


def _seq(span: dict[str, Any]) -> int:
    try:
        return int((span.get("attributes") or {}).get("replay.seq") or 0)
    except (TypeError, ValueError):
        return 0


def _order_key(span: dict[str, Any]) -> tuple[str, int, int, str]:
    """Time order, then SDK sequence, then (for LLM spans) history length: in an agent loop a later
    call always carries a longer message history, which breaks ties from coarse clocks."""
    history = 0
    if span.get("kind") == "llm":
        data = parse_json_maybe(span.get("input"))
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            history = len(data["messages"])
    return (str(span.get("start_time") or ""), _seq(span), history, str(span.get("span_id") or ""))


def build_recording(trace: dict[str, Any], spans: list[dict[str, Any]]) -> Recording:
    """``spans`` must have payloads resolved (no $refs)."""
    spans = sorted(spans, key=_order_key)
    steps: list[LLMCall] = []
    tool_events: list[ToolEvent] = []
    prompt_vars: dict[str, Any] | None = None
    notes: list[str] = []
    for span in spans:
        attrs = span.get("attributes") or {}
        pv = parse_json_maybe(attrs.get("replay.prompt.variables"))
        if isinstance(pv, dict) and prompt_vars is None:
            prompt_vars = pv
        if span.get("kind") == "llm":
            steps.append(extract_llm_call(span.get("input"), span.get("output"), attrs, span.get("model")))
        elif span.get("kind") in ("tool", "retrieval"):
            tool_events.append(_tool_from_span(len(tool_events), span))
    derived = _events_from_messages(steps, len(tool_events), tool_events)
    if derived:
        notes.append(f"{len(derived)} tool result(s) recovered from LLM message history")
    tool_events.extend(derived)
    if not steps:
        notes.append("no LLM spans: not replayable")
    final = steps[-1].output if steps else None
    if steps and final is None:
        notes.append("final LLM span has no recorded output")
    multimodal = any(s.multimodal for s in steps)
    if multimodal:
        notes.append("contains non-text content that replay replaces with placeholders")
    preview = None
    if steps:
        users = [m for m in steps[0].messages if m.get("role") == "user" and m.get("content")]
        preview = str(users[-1]["content"])[:500] if users else None
    return Recording(
        trace_external_id=str(trace.get("trace_id") or trace.get("external_id") or ""),
        llm_steps=steps,
        tool_events=tool_events,
        final_output=final,
        prompt_variables=prompt_vars,
        input_preview=preview or trace.get("input_preview"),
        recorded_latency_ms=trace.get("duration_ms"),
        recorded_cost_usd=trace.get("cost_usd"),
        multimodal=multimodal,
        notes=notes,
    )
