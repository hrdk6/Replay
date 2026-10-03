"""A small tool-using support agent instrumented with the Replay SDK.

Runs against OpenAI when OPENAI_API_KEY is set, otherwise against a scripted
offline "LLM" so it works with no keys (useful for demos and tests).

    REPLAY_API_KEY=rk_... REPLAY_ENDPOINT=http://localhost:8000 python examples/support_agent/agent.py
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from typing import Any

import replay_sdk as replay

ORDERS = {
    "A100": {"status": "shipped", "eta": "2026-10-07", "carrier": "UPS"},
    "B200": {"status": "processing", "eta": "2026-10-12", "carrier": None},
    "C300": {"status": "delivered", "eta": None, "carrier": "FedEx"},
    "D400": {"status": "cancelled", "eta": None, "carrier": None},
}
KB = {
    "refund": "Refunds are issued to the original payment method within 5-7 business days.",
    "shipping": "Standard shipping takes 3-5 business days; express takes 1-2.",
    "cancel": "Orders can be cancelled until they ship.",
}
SYSTEM = "You are a concise support agent for Acme. Use tools to look up orders and policies before answering."
TOOLS = [
    {"type": "function", "function": {"name": "get_order", "description": "Look up an order by id",
                                      "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                                                     "required": ["order_id"]}}},
    {"type": "function", "function": {"name": "search_kb", "description": "Search the help center",
                                      "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                                                     "required": ["query"]}}},
]


@replay.tool()
def get_order(order_id: str) -> dict[str, Any]:
    order = ORDERS.get(order_id.upper())
    return {"order_id": order_id.upper(), **order} if order else {"error": "order not found"}


@replay.retriever()
def search_kb(query: str) -> list[str]:
    return [text for key, text in KB.items() if key in query.lower()] or ["No matching article."]


TOOL_FNS = {"get_order": get_order, "search_kb": search_kb}


class ScriptedLLM:
    """Offline stand-in shaped like the OpenAI client (chat.completions.create)."""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        last = messages[-1]
        if last["role"] == "user":
            q = last["content"]
            m = re.search(r"\b([A-D]\d00)\b", q.upper())
            call = ("get_order", {"order_id": m.group(1)}) if m else ("search_kb", {"query": q})
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": call[0], "arguments": json.dumps(call[1])}}]}
        else:
            result = last["content"]
            msg = {"role": "assistant", "content": f"Here's what I found: {result}"}
        usage = {"prompt_tokens": sum(len(str(x.get('content') or '')) for x in messages) // 4 + 20,
                 "completion_tokens": len(str(msg.get("content") or "")) // 4 + 5}
        return _Resp({"model": model, "choices": [{"message": msg, "finish_reason": "stop"}], "usage": usage})


class _Resp:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.choices = [SimpleNamespace(message=SimpleNamespace(**{**{"tool_calls": None}, **data["choices"][0]["message"]}))]

    def model_dump(self) -> dict[str, Any]:
        return self._data


def make_client() -> Any:
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI

        return replay.wrap_openai(OpenAI())
    return replay.wrap_openai(ScriptedLLM())


def _tool_calls(message: Any) -> list[Any]:
    return list(getattr(message, "tool_calls", None) or [])


@replay.trace("support_agent")
def answer(client: Any, question: str, model: str = "gpt-4o-mini", route: str = "/support") -> str:
    current = replay.current_span()
    if current is not None:
        current.set_trace_metadata(tags=["support"], metadata={"route": route})
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    for _ in range(4):
        resp = client.chat.completions.create(model=model, messages=messages, tools=TOOLS, temperature=0.2)
        message = resp.choices[0].message
        calls = _tool_calls(message)
        if not calls:
            return str(message.content)
        messages.append({"role": "assistant", "content": message.content, "tool_calls": [
            {"id": c["id"] if isinstance(c, dict) else c.id, "type": "function",
             "function": c["function"] if isinstance(c, dict) else {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]})
        for c in calls:
            fn = c["function"] if isinstance(c, dict) else {"name": c.function.name, "arguments": c.function.arguments}
            result = TOOL_FNS[fn["name"]](**json.loads(fn["arguments"]))
            messages.append({"role": "tool", "tool_call_id": c["id"] if isinstance(c, dict) else c.id,
                             "content": json.dumps(result)})
    return "Sorry, I couldn't finish that."


QUESTIONS = [
    "Where is my order A100?", "What's the status of B200?", "Has C300 arrived?", "Why was D400 cancelled?",
    "How do refunds work?", "How long does shipping take?", "Can I cancel my order?", "Where is order A100 now?",
]

if __name__ == "__main__":
    replay.init()
    client = make_client()
    for i, q in enumerate(QUESTIONS * 5):
        print(answer(client, q, route="/billing" if "refund" in q.lower() else "/support"))
    replay.flush()
    print(replay.stats())
