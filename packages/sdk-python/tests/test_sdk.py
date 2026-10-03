"""SDK tests against a local HTTP collector. No external services needed."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
import replay_sdk as replay

pytestmark = pytest.mark.no_clean


class Collector:
    def __init__(self, status: int = 202, delay: float = 0.0) -> None:
        self.batches: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.status = status
        self.delay = delay
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                if collector.delay:
                    time.sleep(collector.delay)
                collector.headers.append(dict(self.headers))
                collector.batches.append(json.loads(body))
                self.send_response(collector.status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def spans(self) -> list[dict[str, Any]]:
        return [s for b in self.batches for s in b["spans"]]

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def collector() -> Iterator[Collector]:
    c = Collector()
    replay.init(api_key="rk_test", endpoint=c.url, flush_interval=0.05)
    yield c
    replay.shutdown()
    c.close()


def test_nested_spans_share_trace_and_link_parents(collector: Collector) -> None:
    @replay.tool()
    def get_order(order_id: str) -> dict[str, str]:
        return {"order_id": order_id, "status": "shipped"}

    @replay.trace(tags=["support"], metadata={"route": "/support"})
    def handle(question: str) -> str:
        with replay.span("plan", kind="chain") as s:
            s.set_attribute("step", 1)
            get_order("A100")
        return "done"

    assert handle("where is A100?") == "done"
    assert replay.flush(5)
    spans = {s["name"]: s for s in collector.spans}
    root = spans[next(n for n in spans if n.endswith("handle"))]
    assert root["parent_span_id"] is None and root["kind"] == "agent"
    assert root["input"] == {"question": "where is A100?"} and root["output"] == "done"
    plan, tool = spans["plan"], spans[next(n for n in spans if n.endswith("get_order"))]
    assert plan["parent_span_id"] == root["span_id"] and tool["parent_span_id"] == plan["span_id"]
    assert len({s["trace_id"] for s in collector.spans}) == 1
    assert tool["input"] == {"name": tool["name"], "arguments": {"order_id": "A100"}}
    assert plan["attributes"]["step"] == 1 and plan["attributes"]["replay.seq"] > 0
    meta = collector.batches[-1]["traces"]
    assert meta and meta[0]["tags"] == ["support"] and meta[0]["metadata"] == {"route": "/support"}
    assert collector.headers[0]["Authorization"] == "Bearer rk_test"


def test_exceptions_are_recorded_and_reraised(collector: Collector) -> None:
    @replay.trace()
    def boom() -> None:
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        boom()
    replay.flush(5)
    span = collector.spans[0]
    assert span["status"] == "error" and "ValueError: bad input" in span["status_message"]


def test_async_functions(collector: Collector) -> None:
    @replay.retriever("kb")
    async def search(query: str) -> list[str]:
        await asyncio.sleep(0)
        return ["doc1", "doc2"]

    @replay.trace("agent")
    async def run() -> list[str]:
        return await search("refund")

    assert asyncio.run(run()) == ["doc1", "doc2"]
    replay.flush(5)
    kinds = {s["name"]: s["kind"] for s in collector.spans}
    assert kinds == {"agent": "agent", "kb": "retrieval"}


def test_concurrent_tasks_keep_separate_traces(collector: Collector) -> None:
    @replay.trace("job")
    async def job(i: int) -> int:
        with replay.span("inner"):
            await asyncio.sleep(0.01)
        return i

    async def main() -> None:
        await asyncio.gather(*(job(i) for i in range(5)))

    asyncio.run(main())
    replay.flush(5)
    roots = [s for s in collector.spans if s["name"] == "job"]
    inners = [s for s in collector.spans if s["name"] == "inner"]
    assert len({r["trace_id"] for r in roots}) == 5
    root_ids = {r["span_id"]: r["trace_id"] for r in roots}
    assert all(root_ids[i["parent_span_id"]] == i["trace_id"] for i in inners)


# --- auto-instrumentation with fake clients ---------------------------------------------


class FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self) -> dict[str, Any]:
        return self._data


def _fake_openai(stream_chunks: Any = None) -> Any:
    def create(**kwargs: Any) -> Any:
        if kwargs.get("stream"):
            return iter(stream_chunks)
        return FakeResponse(
            {
                "model": "gpt-test",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            }
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_wrap_openai_records_llm_span_and_prompt_variables(collector: Collector) -> None:
    client = replay.wrap_openai(_fake_openai())
    replay.wrap_openai(client)  # idempotent
    with replay.prompt_variables(question="q1"):
        out = client.chat.completions.create(
            model="gpt-test", messages=[{"role": "user", "content": "q1"}], temperature=0
        )
    assert out.model_dump()["choices"][0]["message"]["content"] == "hi"
    replay.flush(5)
    span = collector.spans[0]
    assert span["kind"] == "llm" and span["input"]["messages"][0]["content"] == "q1"
    assert span["input"]["temperature"] == 0
    assert span["attributes"]["gen_ai.usage.input_tokens"] == 7
    assert span["attributes"]["gen_ai.response.model"] == "gpt-test"
    assert span["attributes"]["replay.prompt.variables"] == {"question": "q1"}


def test_wrap_openai_streaming_accumulates(collector: Collector) -> None:
    chunks = [
        {"model": "gpt-test", "choices": [{"delta": {"content": "Hel"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "content": "lo",
                        "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get", "arguments": '{"a"'}}],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    client = replay.wrap_openai(_fake_openai(chunks))
    got = list(client.chat.completions.create(model="gpt-test", messages=[], stream=True))
    assert len(got) == 3
    replay.flush(5)
    msg = collector.spans[0]["output"]["choices"][0]["message"]
    assert msg["content"] == "Hello"
    assert msg["tool_calls"][0]["function"] == {"name": "get", "arguments": '{"a": 1}'}


def test_wrap_anthropic_async_and_errors(collector: Collector) -> None:
    class Messages:
        async def create(self, **kwargs: Any) -> Any:
            if kwargs.get("model") == "bad":
                raise RuntimeError("api down")
            return FakeResponse(
                {
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "yo"}],
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                }
            )

    client = replay.wrap_anthropic(SimpleNamespace(messages=Messages()))
    res = asyncio.run(client.messages.create(model="claude-test", system="s", max_tokens=10, messages=[]))
    assert res.model_dump()["content"][0]["text"] == "yo"
    with pytest.raises(RuntimeError):
        asyncio.run(client.messages.create(model="bad", messages=[]))
    replay.flush(5)
    ok, bad = collector.spans
    assert ok["input"]["system"] == "s" and ok["attributes"]["gen_ai.usage.output_tokens"] == 1
    assert bad["status"] == "error"


def test_sync_decorator_returning_coroutine_is_awaited(collector: Collector) -> None:
    async def real_create(**kwargs: Any) -> Any:
        return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "async"}}]})

    def required_args_style(**kwargs: Any) -> Any:  # sync wrapper that returns a coroutine (like the OpenAI SDK)
        return real_create(**kwargs)

    client = replay.wrap_openai(
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=required_args_style)))
    )
    res = asyncio.run(client.chat.completions.create(model="m", messages=[]))
    assert res.model_dump()["choices"][0]["message"]["content"] == "async"
    replay.flush(5)
    assert collector.spans[0]["output"]["choices"][0]["message"]["content"] == "async"
    assert replay.current_span() is None  # no context leaked into the caller


# --- never block, never crash ------------------------------------------------------------------


def test_unreachable_api_never_raises_or_blocks() -> None:
    replay.init(api_key="rk_test", endpoint="http://127.0.0.1:1", flush_interval=0.05, timeout=0.2)
    try:
        start = time.perf_counter()
        for _ in range(200):
            with replay.span("x"):
                pass
        assert time.perf_counter() - start < 1.0
        replay.flush(5)
        assert replay.stats()["dropped"] == 200
    finally:
        replay.shutdown()


def test_full_queue_drops_instead_of_blocking() -> None:
    slow = Collector(delay=1.0)
    replay.init(api_key="rk_test", endpoint=slow.url, max_queue_size=10, batch_size=5, flush_interval=0.01)
    try:
        start = time.perf_counter()
        for _ in range(500):
            with replay.span("x"):
                pass
        assert time.perf_counter() - start < 1.0
        assert replay.stats()["dropped"] > 400
    finally:
        replay.shutdown(timeout=0.1)
        slow.close()


def test_disabled_without_key_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPLAY_API_KEY", raising=False)
    replay.init()

    @replay.trace()
    def f(x: int) -> int:
        return x + 1

    assert f(1) == 2
    assert replay.stats() == {"sent": 0, "dropped": 0, "failed_batches": 0, "queued": 0}


def test_sampling_zero_sends_nothing() -> None:
    c = Collector()
    replay.init(api_key="rk_test", endpoint=c.url, sample_rate=0.0, flush_interval=0.05)
    try:
        with replay.span("root"), replay.span("child"):
            pass
        replay.flush(2)
        assert c.spans == []
    finally:
        replay.shutdown()
        c.close()


def test_client_side_redaction_hook_and_unserializable_values(collector: Collector) -> None:
    replay.init(
        api_key="rk_test",
        endpoint=collector.url,
        flush_interval=0.05,
        redact=lambda v: "[hidden]" if v is not None else None,
    )

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    with replay.span("s", input={"secret": 1}) as s:
        s.set_attribute("obj", Weird())
    replay.flush(5)
    span = collector.spans[0]
    assert span["input"] == "[hidden]" and span["attributes"]["obj"] == "<weird>"
