"""Span ingest: validate -> redact -> offload large payloads -> idempotent upsert.

Idempotency: spans are keyed by (trace, span_id) and traces by
(project, trace_id), so SDK retries and duplicate deliveries overwrite rather
than duplicate. Concurrent batches for the same trace serialise on the trace
row lock taken by the trace upsert, so aggregates stay consistent.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.config import Settings
from replay_api.db.models import Org, Project, Span, Trace
from replay_api.services import usage
from replay_api.services.pricing import cost_usd, price_for
from replay_api.services.redaction import Redactor
from replay_api.services.storage import PayloadStore, dumps, span_payload_key

SpanKind = Literal["llm", "tool", "retrieval", "agent", "chain", "embedding", "other"]
_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")
RESERVED_NAME_KEY = "replay.name"


def _check_id(v: str) -> str:
    if not _ID_RE.match(v):
        raise ValueError("ids may contain only letters, digits and . _ : -")
    return v


def _utc(v: datetime) -> datetime:
    return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


class SpanIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trace_id: str = Field(min_length=1, max_length=128)
    span_id: str = Field(min_length=1, max_length=64)
    parent_span_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=300)
    kind: SpanKind = "other"
    start_time: datetime
    end_time: datetime | None = None
    status: Literal["ok", "error", "unset"] = "ok"
    status_message: str | None = Field(default=None, max_length=4000)
    attributes: dict[str, Any] = Field(default_factory=dict)
    input: Any = None
    output: Any = None

    _ids = field_validator("trace_id", "span_id")(_check_id)

    @field_validator("parent_span_id")
    @classmethod
    def _parent(cls, v: str | None) -> str | None:
        return _check_id(v) if v else None

    @field_validator("start_time", "end_time")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        return _utc(v) if v is not None else None

    @field_validator("attributes")
    @classmethod
    def _attrs(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 256:
            raise ValueError("at most 256 attributes per span")
        for k in v:
            if len(k) > 256:
                raise ValueError("attribute keys must be <= 256 characters")
        return v

    @model_validator(mode="after")
    def _times(self) -> SpanIn:
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time is before start_time")
        return self


class TraceMetaIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trace_id: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _ids = field_validator("trace_id")(_check_id)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = t.strip()
            if not t or len(t) > 64:
                raise ValueError("tags must be 1-64 characters")
            out.append(t)
        return sorted(set(out))

    @field_validator("metadata")
    @classmethod
    def _meta(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 64:
            raise ValueError("at most 64 metadata keys")
        for k, val in v.items():
            if len(k) > 128:
                raise ValueError("metadata keys must be <= 128 characters")
            if not isinstance(val, str | int | float | bool) and val is not None:
                raise ValueError("metadata values must be scalars")
            if isinstance(val, str) and len(val) > 1000:
                raise ValueError("metadata string values must be <= 1000 characters")
        return v


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spans: list[SpanIn] = Field(default_factory=list)
    traces: list[TraceMetaIn] = Field(default_factory=list, max_length=1000)


class IngestResult(BaseModel):
    accepted_spans: int
    traces: int
    new_traces: int
    redactions: dict[str, int]


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _preview(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("messages", "input", "query", "question", "prompt", "output", "content", "text", "answer"):
            if key in value:
                return _preview(value[key], limit)
    if isinstance(value, list) and value:
        last = value[-1]
        if isinstance(last, dict) and "content" in last:
            return _preview(last["content"], limit)
    text_value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text_value[:limit]


def _gen_ai_fields(attrs: dict[str, Any]) -> tuple[str | None, str | None, int | None, int | None]:
    model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
    provider = attrs.get("gen_ai.provider.name") or attrs.get("gen_ai.system")
    in_tok = _int(attrs.get("gen_ai.usage.input_tokens", attrs.get("gen_ai.usage.prompt_tokens")))
    out_tok = _int(attrs.get("gen_ai.usage.output_tokens", attrs.get("gen_ai.usage.completion_tokens")))
    return (str(model)[:200] if model else None, str(provider)[:50] if provider else None, in_tok, out_tok)


async def ingest_batch(
    db: AsyncSession,
    store: PayloadStore,
    settings: Settings,
    org: Org,
    project: Project,
    req: IngestRequest,
) -> IngestResult:
    if not req.spans and not req.traces:
        return IngestResult(accepted_spans=0, traces=0, new_traces=0, redactions={})

    redactor = Redactor.from_config(project.redaction)
    trace_ids = sorted({s.trace_id for s in req.spans} | {t.trace_id for t in req.traces})

    # Quota: count only traces this batch would create; late spans for existing traces are always accepted.
    existing = set(
        (
            await db.execute(
                select(Trace.external_id).where(Trace.project_id == project.id, Trace.external_id.in_(trace_ids))
            )
        ).scalars()
    )
    if len(trace_ids) > len(existing):
        await usage.check_daily_quota(db, org, usage.METRIC_TRACES, len(trace_ids) - len(existing))

    # 1) Upsert trace rows (locks existing rows -> serialises concurrent batches).
    trace_rows = [
        {"id": uuid.uuid4(), "org_id": org.id, "project_id": project.id, "external_id": tid} for tid in trace_ids
    ]
    trace_insert = pg_insert(Trace).values(trace_rows)
    stmt: Any = trace_insert.on_conflict_do_update(
        index_elements=["project_id", "external_id"], set_={"external_id": trace_insert.excluded.external_id}
    ).returning(Trace.id, Trace.external_id, literal_column("(xmax = 0)").label("inserted"))
    result = await db.execute(stmt)
    trace_pk: dict[str, uuid.UUID] = {}
    new_traces = 0
    for row in result:
        trace_pk[row.external_id] = row.id
        new_traces += int(bool(row.inserted))

    # 2) Build span rows: redact, offload, derive gen_ai fields.
    span_rows: list[dict[str, Any]] = []
    previews: dict[str, dict[str, str | None]] = {}
    now = datetime.now(UTC)
    for s in req.spans:
        attrs = redactor.redact(s.attributes)
        payloads: dict[str, Any] = {}
        refs: dict[str, str | None] = {"input": None, "output": None}
        sizes: dict[str, int] = {"input": 0, "output": 0}
        for field_name in ("input", "output"):
            value = redactor.redact(getattr(s, field_name))
            if value is None:
                payloads[field_name] = None
                continue
            blob = dumps(value)
            sizes[field_name] = len(blob)
            if len(blob) > settings.inline_payload_max_bytes:
                key = span_payload_key(org.id, project.id, _short_hash(s.trace_id), _short_hash(s.span_id), field_name)
                await store.put(key, blob)
                refs[field_name] = key
                payloads[field_name] = None
            else:
                payloads[field_name] = value
            if s.parent_span_id is None:
                previews.setdefault(s.trace_id, {})[field_name] = _preview(value)
        model, provider, in_tok, out_tok = _gen_ai_fields(attrs)
        span_cost: Decimal | None = None
        if s.kind == "llm" and model and (in_tok or out_tok):
            span_cost = cost_usd(price_for(model), in_tok, out_tok)
        duration = (s.end_time - s.start_time).total_seconds() * 1000 if s.end_time else None
        span_rows.append(
            {
                "id": uuid.uuid4(),
                "org_id": org.id,
                "project_id": project.id,
                "trace_pk": trace_pk[s.trace_id],
                "span_id": s.span_id,
                "parent_span_id": s.parent_span_id,
                "name": s.name,
                "kind": s.kind,
                "status": s.status,
                "status_message": redactor.redact_text(s.status_message) if s.status_message else None,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "duration_ms": duration,
                "attributes": attrs,
                "input": payloads["input"],
                "output": payloads["output"],
                "input_ref": refs["input"],
                "output_ref": refs["output"],
                "input_bytes": sizes["input"],
                "output_bytes": sizes["output"],
                "model": model,
                "provider": provider,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": span_cost,
                "created_at": now,
            }
        )

    # 3) Upsert spans in chunks (asyncpg caps bind parameters per statement).
    update_cols = (
        [c for c in span_rows[0] if c not in ("id", "org_id", "trace_pk", "span_id", "created_at")] if span_rows else []
    )
    for i in range(0, len(span_rows), 400):
        chunk = span_rows[i : i + 400]
        ins = pg_insert(Span).values(chunk)
        ins = ins.on_conflict_do_update(
            index_elements=["trace_pk", "span_id"], set_={c: ins.excluded[c] for c in update_cols}
        )
        await db.execute(ins)

    # 4) Trace-level metadata.
    for meta in req.traces:
        extra: dict[str, Any] = dict(redactor.redact(meta.metadata))
        if meta.name:
            extra[RESERVED_NAME_KEY] = meta.name
        await db.execute(
            text(
                """
                UPDATE traces SET
                  tags = (SELECT COALESCE(jsonb_agg(DISTINCT t ORDER BY t), '[]'::jsonb)
                          FROM jsonb_array_elements_text(tags || CAST(:tags AS jsonb)) AS t),
                  metadata = metadata || CAST(:meta AS jsonb)
                WHERE id = :id
                """
            ),
            {"id": trace_pk[meta.trace_id], "tags": json.dumps(meta.tags), "meta": json.dumps(extra, default=str)},
        )

    for tid, pv in previews.items():
        await db.execute(
            text(
                "UPDATE traces SET input_preview = COALESCE(:i, input_preview), "
                "output_preview = COALESCE(:o, output_preview) WHERE id = :id"
            ),
            {"id": trace_pk[tid], "i": pv.get("input"), "o": pv.get("output")},
        )

    # 5) Recompute aggregates for touched traces.
    await db.execute(
        text(
            """
            UPDATE traces t SET
              span_count = a.cnt,
              error_count = a.errs,
              llm_call_count = a.llm,
              tool_call_count = a.tools,
              start_time = a.st,
              end_time = a.et,
              duration_ms = EXTRACT(EPOCH FROM (a.et - a.st)) * 1000,
              input_tokens = a.itok,
              output_tokens = a.otok,
              cost_usd = a.cost,
              status = CASE WHEN a.errs > 0 THEN 'error' ELSE 'ok' END,
              model = COALESCE(a.model, t.model),
              name = COALESCE(t.metadata->>'replay.name', a.root_name, a.first_name)
            FROM (
              SELECT trace_pk,
                     count(*) AS cnt,
                     count(*) FILTER (WHERE status = 'error') AS errs,
                     count(*) FILTER (WHERE kind = 'llm') AS llm,
                     count(*) FILTER (WHERE kind IN ('tool', 'retrieval')) AS tools,
                     min(start_time) AS st,
                     GREATEST(max(end_time), max(start_time)) AS et,
                     COALESCE(sum(input_tokens), 0) AS itok,
                     COALESCE(sum(output_tokens), 0) AS otok,
                     sum(cost_usd) AS cost,
                     (array_agg(model ORDER BY start_time)
                        FILTER (WHERE kind = 'llm' AND model IS NOT NULL))[1] AS model,
                     (array_agg(name ORDER BY start_time) FILTER (WHERE parent_span_id IS NULL))[1] AS root_name,
                     (array_agg(name ORDER BY start_time))[1] AS first_name
              FROM spans WHERE trace_pk = ANY(:ids) GROUP BY trace_pk
            ) a
            WHERE t.id = a.trace_pk
            """
        ),
        {"ids": list(trace_pk.values())},
    )

    await usage.increment(db, org.id, usage.METRIC_TRACES, new_traces)
    await usage.increment(db, org.id, usage.METRIC_SPANS, len(span_rows))
    total_redactions = sum(redactor.counts.values())
    if total_redactions:
        await usage.increment(db, org.id, usage.METRIC_REDACTIONS, total_redactions)

    return IngestResult(
        accepted_spans=len(span_rows),
        traces=len(trace_pk),
        new_traces=new_traces,
        redactions=dict(redactor.counts),
    )
