"""Replay SDK: capture LLM and agent traces so changes can be tested against real traffic.

Quickstart::

    import replay_sdk as replay
    from openai import OpenAI

    replay.init()                         # reads REPLAY_API_KEY / REPLAY_ENDPOINT
    client = replay.wrap_openai(OpenAI())

    @replay.tool()
    def get_order(order_id: str) -> dict: ...

    @replay.trace(tags=["support"], metadata={"route": "/support"})
    def answer(question: str) -> str: ...

The SDK never blocks or raises into your application: spans are queued in a
bounded buffer and sent by a background thread; if the buffer is full or the
API is unreachable, data is dropped (see ``replay.stats()``).
"""

from replay_sdk._core import (
    Span,
    current_span,
    flush,
    init,
    prompt_variables,
    retriever,
    shutdown,
    span,
    stats,
    tool,
    trace,
)
from replay_sdk.integrations import instrument, wrap_anthropic, wrap_openai

__version__ = "0.1.0"

__all__ = [
    "Span",
    "current_span",
    "flush",
    "init",
    "instrument",
    "prompt_variables",
    "retriever",
    "shutdown",
    "span",
    "stats",
    "tool",
    "trace",
    "wrap_anthropic",
    "wrap_openai",
]
