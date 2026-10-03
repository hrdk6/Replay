# API overview

The interactive OpenAPI docs are at `/docs` in development (disabled in production). Errors always look
like this:

```json
{"error": {"code": "not_found", "message": "trace not found", "details": "..."}}
```

`details` is optional. Validation errors list each failing location and message, never the value that
was submitted.

## API-key endpoints (SDK, OTLP, CI)

Send `Authorization: Bearer rk_…`. A key can only read and write its own project.

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/ingest` | Batched spans and optional trace metadata. Returns 202 with `{accepted_spans, traces, new_traces, redactions}`. Re-sending a span is safe (upsert). |
| POST | `/v1/otlp/v1/traces` | OTLP/HTTP, JSON or protobuf. Set your exporter's traces endpoint to this URL. |
| GET | `/v1/project` | Which project this key belongs to |
| GET | `/v1/traces/{trace_id}` | Look up a trace by the id you sent |
| GET | `/v1/datasets`, `/v1/judges` | Lists, for CI configuration |
| POST | `/v1/ci/experiments` | Start an experiment from candidate and baseline configs |
| GET | `/v1/ci/experiments/{id}` | Status, progress, full report, dashboard URL |

### Ingest format

```json
{
  "spans": [{
    "trace_id": "req-123",                  // your id, [A-Za-z0-9._:-]{1,128}
    "span_id": "llm-1",
    "parent_span_id": null,
    "name": "chat",
    "kind": "llm",                          // llm | tool | retrieval | agent | chain | embedding | other
    "start_time": "2026-10-03T12:00:00Z",
    "end_time": "2026-10-03T12:00:01Z",
    "status": "ok",                         // ok | error | unset
    "attributes": {"gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 120},
    "input": {"messages": [...], "tools": [...], "model": "gpt-4o-mini", "temperature": 0.2},
    "output": {"role": "assistant", "content": "..."}
  }],
  "traces": [{"trace_id": "req-123", "name": "answer", "tags": ["support"], "metadata": {"route": "/support"}}]
}
```

For **LLM spans**, `input` may be our canonical shape, an OpenAI or Anthropic request body, or a list
of messages. `output` may be a provider response, a message, or text.

For **tool and retrieval spans**, use `input: {"name": "...", "arguments": {...}}` and set `output` to
the result. That is what replay serves back.

To let candidates change prompt templates, set the attribute `replay.prompt.variables` to the variables
the prompt was rendered from.

## Dashboard endpoints

These live under `/api/...`, use the session cookie, and need an `X-CSRF-Token` header on unsafe
methods. They are meant for the dashboard, not as a public API, and may change without notice.
