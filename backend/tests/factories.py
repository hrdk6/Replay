"""Test data builders: realistic agent traces in the ingest format."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

ORDERS = {
    "A100": {"status": "shipped", "eta": "2026-10-07", "carrier": "UPS"},
    "B200": {"status": "processing", "eta": "2026-10-12", "carrier": None},
    "C300": {"status": "delivered", "eta": None, "carrier": "FedEx"},
    "D400": {"status": "cancelled", "eta": None, "carrier": None},
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look up an order by id",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "Search the help center",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
]
SYSTEM = "You are a concise support agent for Acme. Use tools to look up orders before answering."


def agent_trace(
    i: int,
    *,
    order_id: str | None = None,
    tags: list[str] | None = None,
    route: str = "/support/order",
    model: str = "gpt-4o-mini",
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Two LLM steps around one tool call, OpenAI-style payloads."""
    order_id = order_id or list(ORDERS)[i % len(ORDERS)]
    order = ORDERS[order_id]
    tid = trace_id or f"trace-{uuid.uuid4().hex[:16]}"
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC) + timedelta(minutes=i)
    question = f"Hi, where is my order {order_id}? My email is jane.doe{i}@example.com"
    call_id = f"call_{i}"
    msgs1 = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    out1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "get_order", "arguments": f'{{"order_id": "{order_id}"}}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 20},
    }
    tool_result = {"order_id": order_id, **order}
    msgs2 = [
        *msgs1,
        {"role": "assistant", "content": None, "tool_calls": out1["choices"][0]["message"]["tool_calls"]},  # type: ignore[index]
        {"role": "tool", "tool_call_id": call_id, "content": str(tool_result)},
    ]
    answer = f"Your order {order_id} is {order['status']}." + (
        f" Expected delivery {order['eta']} via {order['carrier']}." if order["eta"] else ""
    )
    spans = [
        {
            "trace_id": tid,
            "span_id": f"{tid}-root",
            "name": "support_agent",
            "kind": "agent",
            "start_time": t0.isoformat(),
            "end_time": (t0 + timedelta(seconds=3)).isoformat(),
            "input": {"question": question},
            "output": answer,
        },
        {
            "trace_id": tid,
            "span_id": f"{tid}-llm1",
            "parent_span_id": f"{tid}-root",
            "name": "chat",
            "kind": "llm",
            "start_time": (t0 + timedelta(milliseconds=10)).isoformat(),
            "end_time": (t0 + timedelta(seconds=1)).isoformat(),
            "attributes": {
                "gen_ai.request.model": model,
                "gen_ai.system": "openai",
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.request.temperature": 0.2,
            },
            "input": {"messages": msgs1, "tools": TOOLS, "model": model, "temperature": 0.2},
            "output": out1,
        },
        {
            "trace_id": tid,
            "span_id": f"{tid}-tool1",
            "parent_span_id": f"{tid}-root",
            "name": "get_order",
            "kind": "tool",
            "start_time": (t0 + timedelta(seconds=1)).isoformat(),
            "end_time": (t0 + timedelta(seconds=1, milliseconds=200)).isoformat(),
            "input": {"name": "get_order", "arguments": {"order_id": order_id}, "call_id": call_id},
            "output": tool_result,
        },
        {
            "trace_id": tid,
            "span_id": f"{tid}-llm2",
            "parent_span_id": f"{tid}-root",
            "name": "chat",
            "kind": "llm",
            "start_time": (t0 + timedelta(seconds=1, milliseconds=300)).isoformat(),
            "end_time": (t0 + timedelta(seconds=3)).isoformat(),
            "attributes": {
                "gen_ai.request.model": model,
                "gen_ai.system": "openai",
                "gen_ai.usage.input_tokens": 180,
                "gen_ai.usage.output_tokens": 30,
            },
            "input": {"messages": msgs2, "tools": TOOLS, "model": model},
            "output": {
                "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 180, "completion_tokens": 30},
            },
        },
    ]
    return {
        "spans": spans,
        "traces": [{"trace_id": tid, "tags": tags or ["support"], "metadata": {"route": route}}],
    }


def merge(*batches: dict[str, Any]) -> dict[str, Any]:
    return {"spans": [s for b in batches for s in b["spans"]], "traces": [t for b in batches for t in b["traces"]]}
