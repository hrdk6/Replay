"""OTLP/HTTP trace ingest (JSON and protobuf) mapped onto our span model.

Understands, in priority order:
* OpenTelemetry GenAI semantic conventions (``gen_ai.input.messages``,
  ``gen_ai.output.messages``, ``gen_ai.system_instructions``, tool attributes,
  and the older ``gen_ai.*.message`` events),
* OpenLLMetry indexed attributes (``gen_ai.prompt.N.role/content``),
* OpenInference attributes (``llm.input_messages.N.message.*``, ``input.value``).
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

from replay_api.replay.canonical import normalize_messages, normalize_output, parse_json_maybe
from replay_api.services.ingest import IngestRequest, SpanIn, TraceMetaIn

CONTENT_ATTRS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "input.value",
    "output.value",
)
_INDEXED = re.compile(r"^(gen_ai\.(?:prompt|completion)|llm\.(?:input|output)_messages)\.(\d+)\.(.+)$")


def any_value(v: dict[str, Any] | None) -> Any:
    if not v:
        return None
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "arrayValue" in v:
        return [any_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv["key"]: any_value(kv.get("value")) for kv in v["kvlistValue"].get("values", [])}
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def attrs_to_dict(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {a["key"]: any_value(a.get("value")) for a in attrs or [] if "key" in a}


def _ns_to_dt(ns: Any) -> datetime | None:
    if ns in (None, "", 0, "0"):
        return None
    return datetime.fromtimestamp(int(ns) / 1e9, tz=UTC)


def _id(value: Any, from_proto: bool) -> str:
    if not value:
        return ""
    if from_proto:
        return base64.b64decode(value).hex()
    return str(value).lower()


def _span_kind(attrs: dict[str, Any], name: str) -> str:
    op = str(attrs.get("gen_ai.operation.name", "")).lower()
    if op in ("chat", "text_completion", "generate_content", "completion"):
        return "llm"
    if op == "execute_tool" or "gen_ai.tool.name" in attrs:
        return "tool"
    if op in ("embeddings", "embedding"):
        return "embedding"
    if op in ("invoke_agent", "create_agent"):
        return "agent"
    oi = str(attrs.get("openinference.span.kind", "")).upper()
    mapping = {
        "LLM": "llm",
        "TOOL": "tool",
        "RETRIEVER": "retrieval",
        "CHAIN": "chain",
        "AGENT": "agent",
        "EMBEDDING": "embedding",
    }
    if oi in mapping:
        return mapping[oi]
    tl = str(attrs.get("traceloop.span.kind", "")).lower()
    if tl in ("tool",):
        return "tool"
    if tl in ("agent",):
        return "agent"
    if tl in ("workflow", "task"):
        return "chain"
    if any(k.startswith("gen_ai.prompt.") or k.startswith("llm.input_messages.") for k in attrs):
        return "llm"
    if "retriev" in name.lower():
        return "retrieval"
    return "other"


def _indexed_messages(attrs: dict[str, Any], base: str) -> list[dict[str, Any]]:
    """Collect gen_ai.prompt.N.* / llm.input_messages.N.message.* into message dicts."""
    msgs: dict[int, dict[str, Any]] = {}
    for key, value in attrs.items():
        m = _INDEXED.match(key)
        if not m or m.group(1) != base:
            continue
        idx, rest = int(m.group(2)), m.group(3)
        rest = rest.removeprefix("message.")
        msg = msgs.setdefault(idx, {})
        if rest in ("role", "content"):
            msg[rest] = value
        elif rest.startswith("tool_calls."):
            parts = rest.split(".")
            if len(parts) >= 3:
                call_idx, field = int(parts[1]), parts[-1]
                calls = msg.setdefault("_calls", {})
                calls.setdefault(call_idx, {})[field] = value
        elif rest in ("tool_call_id",):
            msg["tool_call_id"] = value
    out = []
    for _, msg in sorted(msgs.items()):
        calls = msg.pop("_calls", None)
        if calls:
            msg["tool_calls"] = [
                {
                    "id": c.get("id", f"call_{i}"),
                    "name": c.get("name", ""),
                    "arguments": parse_json_maybe(c.get("arguments", {})),
                }
                for i, c in sorted(calls.items())
            ]
        out.append(msg)
    return out


def _events_messages(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
    """Legacy GenAI events: gen_ai.{system,user,assistant,tool}.message and gen_ai.choice."""
    inputs: list[dict[str, Any]] = []
    output: Any = None
    for ev in events:
        name = ev.get("name", "")
        body = attrs_to_dict(ev.get("attributes"))
        if name in ("gen_ai.system.message", "gen_ai.user.message", "gen_ai.assistant.message", "gen_ai.tool.message"):
            role = name.split(".")[1]
            msg: dict[str, Any] = {"role": role, "content": body.get("content", body.get("gen_ai.event.content"))}
            if "tool_calls" in body:
                msg["tool_calls"] = parse_json_maybe(body["tool_calls"])
            if "id" in body:
                msg["tool_call_id"] = body["id"]
            inputs.append(msg)
        elif name == "gen_ai.choice":
            output = body.get("message", body)
        elif name in ("gen_ai.content.prompt",):
            inputs.extend(normalize_messages(parse_json_maybe(body.get("gen_ai.prompt"))))
        elif name in ("gen_ai.content.completion",):
            output = parse_json_maybe(body.get("gen_ai.completion"))
    return inputs, output


def _llm_payload(attrs: dict[str, Any], events: list[dict[str, Any]]) -> tuple[Any, Any]:
    messages: list[dict[str, Any]] = []
    sys_instr = attrs.get("gen_ai.system_instructions")
    if sys_instr:
        parsed = parse_json_maybe(sys_instr)
        text = (
            parsed
            if isinstance(parsed, str)
            else "\n".join(str(p.get("content", "")) for p in parsed if isinstance(p, dict))
            if isinstance(parsed, list)
            else json.dumps(parsed)
        )
        messages.append({"role": "system", "content": text})
    output: Any = None
    if "gen_ai.input.messages" in attrs:
        messages.extend(normalize_messages(attrs["gen_ai.input.messages"]))
        output = normalize_output(attrs.get("gen_ai.output.messages"))
    else:
        indexed_in = _indexed_messages(attrs, "gen_ai.prompt") or _indexed_messages(attrs, "llm.input_messages")
        indexed_out = _indexed_messages(attrs, "gen_ai.completion") or _indexed_messages(attrs, "llm.output_messages")
        if indexed_in:
            messages.extend(normalize_messages(indexed_in))
            output = normalize_output(indexed_out[0]) if indexed_out else None
        else:
            ev_in, ev_out = _events_messages(events)
            messages.extend(normalize_messages(ev_in))
            output = normalize_output(ev_out)
    if not messages and "input.value" in attrs:
        messages = normalize_messages(parse_json_maybe(attrs["input.value"]))
    if output is None and "output.value" in attrs:
        output = normalize_output(attrs["output.value"])
    params = {
        k: attrs[a]
        for k, a in (
            ("temperature", "gen_ai.request.temperature"),
            ("top_p", "gen_ai.request.top_p"),
            ("max_tokens", "gen_ai.request.max_tokens"),
            ("seed", "gen_ai.request.seed"),
        )
        if a in attrs
    }
    tools = parse_json_maybe(attrs.get("gen_ai.tool.definitions")) or []
    payload_in = {
        "messages": messages,
        "tools": tools,
        "model": attrs.get("gen_ai.request.model"),
        "params": params,
    }
    return payload_in, output


def _tool_payload(attrs: dict[str, Any]) -> tuple[Any, Any]:
    args = parse_json_maybe(attrs.get("gen_ai.tool.call.arguments", attrs.get("input.value")))
    result = parse_json_maybe(attrs.get("gen_ai.tool.call.result", attrs.get("output.value")))
    return {
        "name": attrs.get("gen_ai.tool.name") or attrs.get("tool.name"),
        "arguments": args,
        "call_id": attrs.get("gen_ai.tool.call.id"),
    }, result


def convert_otlp(payload: dict[str, Any], from_proto: bool = False) -> IngestRequest:
    spans: list[SpanIn] = []
    traces: dict[str, TraceMetaIn] = {}
    for rs in payload.get("resourceSpans", []):
        res_attrs = attrs_to_dict((rs.get("resource") or {}).get("attributes"))
        service = res_attrs.get("service.name")
        for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])):
            for sp in ss.get("spans", []):
                attrs = attrs_to_dict(sp.get("attributes"))
                trace_id = _id(sp.get("traceId"), from_proto)
                span_id = _id(sp.get("spanId"), from_proto)
                parent = _id(sp.get("parentSpanId"), from_proto) or None
                name = str(sp.get("name") or "span")[:300]
                kind = _span_kind(attrs, name)
                events = sp.get("events", [])
                if kind == "llm":
                    inp, out = _llm_payload(attrs, events)
                elif kind in ("tool", "retrieval"):
                    inp, out = _tool_payload(attrs)
                    if kind == "retrieval" and not inp.get("name"):
                        inp["name"] = name
                else:
                    inp = parse_json_maybe(attrs.get("input.value"))
                    out = parse_json_maybe(attrs.get("output.value"))
                stored_attrs = {k: v for k, v in attrs.items() if k not in CONTENT_ATTRS and not _INDEXED.match(k)}
                status = sp.get("status") or {}
                code = status.get("code", 0)
                code_str = str(code).upper()
                status_value = "error" if code_str in ("2", "STATUS_CODE_ERROR") else "ok"
                start = _ns_to_dt(sp.get("startTimeUnixNano")) or datetime.now(UTC)
                spans.append(
                    SpanIn(
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=parent,
                        name=name,
                        kind=kind,
                        start_time=start,
                        end_time=_ns_to_dt(sp.get("endTimeUnixNano")),
                        status=status_value,
                        status_message=(status.get("message") or None),
                        attributes=stored_attrs,
                        input=inp,
                        output=out,
                    )
                )
                if trace_id not in traces:
                    meta: dict[str, Any] = {}
                    if service:
                        meta["service.name"] = str(service)[:200]
                    raw_tags = attrs.get("replay.tags") or res_attrs.get("replay.tags")
                    tags = raw_tags if isinstance(raw_tags, list) else str(raw_tags).split(",") if raw_tags else []
                    traces[trace_id] = TraceMetaIn(
                        trace_id=trace_id, tags=[str(t) for t in tags if str(t).strip()], metadata=meta
                    )
    return IngestRequest(spans=spans, traces=list(traces.values()))


def parse_protobuf(body: bytes) -> dict[str, Any]:
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    msg = ExportTraceServiceRequest()
    msg.ParseFromString(body)
    return MessageToDict(msg)


def empty_protobuf_response() -> bytes:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

    return bytes(ExportTraceServiceResponse().SerializeToString())
