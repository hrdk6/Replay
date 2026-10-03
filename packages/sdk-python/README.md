# replay-sdk

Capture LLM and agent traces for [Replay](../../README.md), so you can test model, prompt and retrieval
changes against real traffic before shipping them.

- **Zero dependencies.** Python 3.9 or newer.
- **Never blocks your app.** Spans go into a bounded in-memory queue and are sent by a background thread
  in batches, with 3-second timeouts and at most one retry.
- **Never crashes your app.** Instrumentation errors are swallowed; your own exceptions always propagate
  unchanged. If the queue is full or the API is down, data is dropped and counted in `replay.stats()`.

```bash
pip install replay-sdk
export REPLAY_API_KEY=rk_...
export REPLAY_ENDPOINT=https://api.your-replay-host
```

```python
import replay_sdk as replay
from openai import OpenAI

replay.init()
client = replay.wrap_openai(OpenAI())  # or replay.wrap_anthropic(Anthropic()), or replay.instrument()


@replay.tool()  # arguments and result are recorded; replay serves them back
def get_order(order_id: str) -> dict: ...


@replay.retriever()  # retrieval results can be truncated by candidates (top_k)
def search_docs(query: str) -> list[str]: ...


@replay.trace(tags=["support"], metadata={"route": "/support"})  # tags and metadata become result slices
def answer(question: str) -> str:
    with replay.prompt_variables(question=question):  # lets candidates change the template
        resp = client.chat.completions.create(model="gpt-4o-mini", messages=[...], tools=[...])
    ...
```

| API | Notes |
|---|---|
| `replay.init(api_key=None, endpoint=None, *, sample_rate=1.0, max_queue_size=10_000, batch_size=100, flush_interval=1.0, timeout=3.0, redact=None, capture_content=True, tags=None)` | With no key the SDK is a no-op. `redact(value) -> value` runs on inputs and outputs before they leave the process. |
| `@replay.trace`, `@replay.tool`, `@replay.retriever` | Sync, async and generator functions |
| `with replay.span(name, kind=..., input=...) as s:` | Use `s.set_output()`, `s.set_attribute()` and `s.set_trace_metadata()` |
| `replay.wrap_openai(client)` / `replay.wrap_anthropic(client)` | Instance-level; streaming supported |
| `replay.instrument()` | Patches the installed `openai` and `anthropic` classes globally |
| `replay.flush(timeout)` / `replay.shutdown()` | Call `flush` before short-lived processes exit; it also runs automatically at exit |
| `replay.stats()` | `{"sent", "dropped", "failed_batches", "queued"}` |

Not instrumented in v1: Anthropic's `messages.stream(...)` helper (use `create(stream=True)`) and the
OpenAI Responses API (send spans with `replay.span` instead).
