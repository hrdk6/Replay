"""Canonical chat format and converters from provider/OTel shapes.

Canonical message::

    {"role": "system" | "user" | "assistant" | "tool",
     "content": str | None,
     "tool_calls": [{"id": str, "name": str, "arguments": dict}],   # assistant only
     "tool_call_id": str}                                           # tool only

Accepted inputs: our canonical shape, OpenAI Chat Completions, Anthropic
Messages, and OpenTelemetry GenAI ``parts`` messages. Non-text content
(images, audio) is replaced with a placeholder - multimodal replay is not
supported in v1 and such items are flagged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PARAM_KEYS = ("temperature", "top_p", "max_tokens", "seed", "stop", "effort")


@dataclass
class LLMCall:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] | None = None  # canonical assistant message
    multimodal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "tools": self.tools,
            "model": self.model,
            "provider": self.provider,
            "params": self.params,
            "output": self.output,
            "multimodal": self.multimodal,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMCall:
        return cls(
            messages=list(d.get("messages") or []),
            tools=list(d.get("tools") or []),
            model=d.get("model"),
            provider=d.get("provider"),
            params=dict(d.get("params") or {}),
            output=d.get("output"),
            multimodal=bool(d.get("multimodal", False)),
        )


def parse_json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return value
    return value


def _args(value: Any) -> dict[str, Any]:
    value = parse_json_maybe(value)
    if isinstance(value, dict):
        return value
    if value is None or value == "":
        return {}
    return {"_raw": value}


def content_to_text(content: Any, flags: dict[str, bool] | None = None) -> str | None:
    """Flatten provider content (string or parts/blocks) to text."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text", "output_text") or (ptype is None and "text" in part):
                text = part.get("text", part.get("content"))
                if isinstance(text, str):
                    out.append(text)
            elif ptype in ("thinking", "redacted_thinking", "reasoning"):
                continue  # model-bound reasoning is never replayed
            elif ptype in ("tool_use", "tool_call", "tool_result", "tool_call_response", "function_call"):
                continue  # handled structurally
            else:
                if flags is not None:
                    flags["multimodal"] = True
                out.append(f"[{ptype or 'content'} omitted]")
        return "\n".join(out) if out else None
    return json.dumps(content, ensure_ascii=False, default=str)


def _tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    text = content_to_text(value) if isinstance(value, list) else None
    if text is not None:
        return text
    return json.dumps(value, ensure_ascii=False, default=str)


def _openai_tool_calls(raw: Any) -> list[dict[str, Any]]:
    calls = []
    for i, tc in enumerate(raw or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        if isinstance(fn, dict):
            calls.append(
                {
                    "id": str(tc.get("id") or f"call_{i}"),
                    "name": str(fn.get("name", "")),
                    "arguments": _args(fn.get("arguments")),
                }
            )
        elif "name" in tc:  # canonical
            calls.append(
                {
                    "id": str(tc.get("id") or f"call_{i}"),
                    "name": str(tc["name"]),
                    "arguments": _args(tc.get("arguments", tc.get("input"))),
                }
            )
    return calls


def normalize_message(msg: Any, flags: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """One provider message -> one or more canonical messages."""
    if isinstance(msg, str):
        return [{"role": "user", "content": msg}]
    if not isinstance(msg, dict):
        return []
    role = str(msg.get("role", "user"))
    if role == "developer":
        role = "system"
    if role == "function":
        role = "tool"

    # OpenTelemetry GenAI "parts" format.
    if "parts" in msg and isinstance(msg["parts"], list):
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for i, p in enumerate(msg["parts"]):
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t == "text":
                texts.append(str(p.get("content", "")))
            elif t == "tool_call":
                calls.append(
                    {
                        "id": str(p.get("id") or f"call_{i}"),
                        "name": str(p.get("name", "")),
                        "arguments": _args(p.get("arguments")),
                    }
                )
            elif t == "tool_call_response":
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(p.get("id", "")),
                        "content": _tool_result_text(p.get("result", p.get("response"))),
                    }
                )
            elif t in ("reasoning", "thinking"):
                continue
            else:
                if flags is not None:
                    flags["multimodal"] = True
                texts.append(f"[{t} omitted]")
        out: list[dict[str, Any]] = []
        if results:
            out.extend(results)
        if texts or calls or not results:
            m: dict[str, Any] = {
                "role": role if role != "tool" else "user",
                "content": "\n".join(texts) if texts else None,
            }
            if calls:
                m["role"] = "assistant"
                m["tool_calls"] = calls
            if role == "tool" and not results:
                m = {"role": "tool", "tool_call_id": str(msg.get("tool_call_id", "")), "content": "\n".join(texts)}
            if m.get("content") is not None or m.get("tool_calls") or m["role"] == "tool":
                out.append(m)
        return out

    content = msg.get("content")

    # Anthropic content blocks may carry tool_use / tool_result.
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for b in content
    ):
        out = []
        tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        for b in tool_results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(b.get("tool_use_id", "")),
                    "content": _tool_result_text(b.get("content")),
                }
            )
        text = content_to_text(
            [b for b in content if isinstance(b, dict) and b.get("type") not in ("tool_use", "tool_result")], flags
        )
        if tool_uses:
            out.append(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": str(b.get("id", f"call_{i}")),
                            "name": str(b.get("name", "")),
                            "arguments": _args(b.get("input")),
                        }
                        for i, b in enumerate(tool_uses)
                    ],
                }
            )
        elif text:
            out.append({"role": role, "content": text})
        return out

    m = {"role": role, "content": content_to_text(content, flags)}
    calls = _openai_tool_calls(msg.get("tool_calls"))
    if calls:
        m["tool_calls"] = calls
    if role == "tool":
        m["tool_call_id"] = str(msg.get("tool_call_id") or msg.get("id") or "")
        if m["content"] is None:
            m["content"] = ""
    return [m]


def normalize_messages(raw: Any, flags: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    raw = parse_json_maybe(raw)
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [] if raw is None else [{"role": "user", "content": str(raw)}]
    out: list[dict[str, Any]] = []
    for m in raw:
        out.extend(normalize_message(m, flags))
    return out


def normalize_tools(raw: Any) -> list[dict[str, Any]]:
    raw = parse_json_maybe(raw)
    tools: list[dict[str, Any]] = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        src = fn or t
        name = src.get("name")
        if not name:
            continue
        tools.append(
            {
                "name": str(name),
                "description": str(src.get("description") or ""),
                "parameters": src.get("parameters") or src.get("input_schema") or {"type": "object", "properties": {}},
            }
        )
    return tools


def normalize_output(raw: Any, flags: dict[str, bool] | None = None) -> dict[str, Any] | None:
    """Recorded LLM output (provider response, OTel output messages, or text) -> assistant message."""
    raw = parse_json_maybe(raw)
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"role": "assistant", "content": raw}
    if isinstance(raw, list):
        msgs = normalize_messages(raw, flags)
        assistant = [m for m in msgs if m["role"] == "assistant"]
        return assistant[0] if assistant else (msgs[0] if msgs else None)
    if isinstance(raw, dict):
        if isinstance(raw.get("choices"), list) and raw["choices"]:
            choice = raw["choices"][0]
            msg = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(msg, dict):
                out = normalize_message({**msg, "role": "assistant"}, flags)
                return out[0] if out else {"role": "assistant", "content": None}
        if raw.get("type") == "message" or (raw.get("role") == "assistant" and isinstance(raw.get("content"), list)):
            out = normalize_message({"role": "assistant", "content": raw.get("content")}, flags)
            return out[-1] if out else {"role": "assistant", "content": None}
        if "message" in raw and isinstance(raw["message"], dict):
            return normalize_output(raw["message"], flags)
        if "role" in raw or "parts" in raw or "tool_calls" in raw:
            out = normalize_message({**raw, "role": "assistant"}, flags)
            return out[-1] if out else None
        for key in ("output", "text", "content", "answer", "response", "completion"):
            if key in raw and isinstance(raw[key], str | list):
                return normalize_output(raw[key], flags)
        return {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False, default=str)}
    return {"role": "assistant", "content": str(raw)}


def _attr(attrs: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in attrs and attrs[k] is not None:
            return attrs[k]
    return None


def extract_llm_call(
    span_input: Any, span_output: Any, attributes: dict[str, Any] | None, model: str | None = None
) -> LLMCall:
    """Build a canonical LLMCall from a recorded LLM span."""
    attrs = attributes or {}
    flags: dict[str, bool] = {"multimodal": False}
    data = parse_json_maybe(span_input)
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    params: dict[str, Any] = {}
    rec_model = model

    if isinstance(data, dict) and ("messages" in data or "system" in data or "prompt" in data):
        system = data.get("system")
        if system:
            text = content_to_text(system, flags)
            if text:
                messages.append({"role": "system", "content": text})
        if "messages" in data:
            messages.extend(normalize_messages(data["messages"], flags))
        elif isinstance(data.get("prompt"), str):
            messages.append({"role": "user", "content": data["prompt"]})
        tools = normalize_tools(data.get("tools"))
        rec_model = data.get("model") or rec_model
        raw_params: dict[str, Any] = data["params"] if isinstance(data.get("params"), dict) else data
        for k in PARAM_KEYS:
            if raw_params.get(k) is not None:
                params[k] = raw_params[k]
        if raw_params.get("max_completion_tokens") is not None and "max_tokens" not in params:
            params["max_tokens"] = raw_params["max_completion_tokens"]
    elif isinstance(data, list | str):
        messages = normalize_messages(data, flags)

    if not messages:
        sys_instr = _attr(attrs, "gen_ai.system_instructions")
        if sys_instr:
            text = content_to_text(parse_json_maybe(sys_instr), flags)
            if text:
                messages.append({"role": "system", "content": text})
        messages.extend(normalize_messages(_attr(attrs, "gen_ai.input.messages"), flags))
    if not tools:
        tools = normalize_tools(_attr(attrs, "gen_ai.tool.definitions", "llm.tools"))

    for key, attr_names in (
        ("temperature", ("gen_ai.request.temperature",)),
        ("top_p", ("gen_ai.request.top_p",)),
        ("max_tokens", ("gen_ai.request.max_tokens",)),
        ("seed", ("gen_ai.request.seed",)),
    ):
        if key not in params:
            v = _attr(attrs, *attr_names)
            if v is not None:
                params[key] = v

    output = normalize_output(span_output, flags)
    if output is None:
        output = normalize_output(_attr(attrs, "gen_ai.output.messages"), flags)

    rec_model = rec_model or _attr(attrs, "gen_ai.request.model", "gen_ai.response.model")
    provider = _attr(attrs, "gen_ai.provider.name", "gen_ai.system")
    return LLMCall(
        messages=messages,
        tools=tools,
        model=str(rec_model) if rec_model else None,
        provider=str(provider).lower() if provider else None,
        params=params,
        output=output,
        multimodal=flags["multimodal"],
    )


def message_text(msg: dict[str, Any] | None) -> str:
    if not msg:
        return ""
    text = msg.get("content") or ""
    calls = msg.get("tool_calls") or []
    if calls:
        rendered = "; ".join(f"{c['name']}({json.dumps(c.get('arguments', {}), sort_keys=True)})" for c in calls)
        text = f"{text}\n[tool calls: {rendered}]" if text else f"[tool calls: {rendered}]"
    return str(text)


def render_conversation(messages: list[dict[str, Any]], limit: int = 12_000) -> str:
    """Human/judge-readable transcript."""
    lines = []
    for m in messages:
        role = m.get("role", "?").upper()
        if m.get("role") == "tool":
            lines.append(f"TOOL RESULT ({m.get('tool_call_id', '')}): {m.get('content', '')}")
        else:
            lines.append(f"{role}: {message_text(m)}")
    text = "\n\n".join(lines)
    if len(text) > limit:
        text = "[...earlier conversation truncated...]\n" + text[-limit:]
    return text
