"""Trace explorer API."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select

from replay_api.db.models import Span, Trace
from replay_api.deps import CurrentUser, UserDB, get_project
from replay_api.errors import bad_request, not_found
from replay_api.services import audit
from replay_api.services.storage import get_store

router = APIRouter(prefix="/api/projects/{project_id}", tags=["traces"])


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def trace_out(t: Trace) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "trace_id": t.external_id,
        "name": t.name,
        "start_time": _iso(t.start_time),
        "end_time": _iso(t.end_time),
        "duration_ms": t.duration_ms,
        "status": t.status,
        "span_count": t.span_count,
        "error_count": t.error_count,
        "llm_call_count": t.llm_call_count,
        "tool_call_count": t.tool_call_count,
        "tags": t.tags,
        "metadata": {k: v for k, v in (t.meta or {}).items() if k != "replay.name"},
        "model": t.model,
        "input_preview": t.input_preview,
        "output_preview": t.output_preview,
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "cost_usd": float(t.cost_usd) if t.cost_usd is not None else None,
        "created_at": _iso(t.created_at),
    }


def span_out(s: Span, project_id: uuid.UUID) -> dict[str, Any]:
    def payload(field: str) -> Any:
        if getattr(s, f"{field}_ref"):
            return {
                "$ref": f"/api/projects/{project_id}/spans/{s.id}/payload/{field}",
                "bytes": getattr(s, f"{field}_bytes"),
            }
        return getattr(s, field)

    return {
        "id": str(s.id),
        "span_id": s.span_id,
        "parent_span_id": s.parent_span_id,
        "name": s.name,
        "kind": s.kind,
        "status": s.status,
        "status_message": s.status_message,
        "start_time": _iso(s.start_time),
        "end_time": _iso(s.end_time),
        "duration_ms": s.duration_ms,
        "attributes": s.attributes,
        "input": payload("input"),
        "output": payload("output"),
        "model": s.model,
        "provider": s.provider,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "cost_usd": float(s.cost_usd) if s.cost_usd is not None else None,
    }


def _encode_cursor(t: Trace) -> str:
    raw = f"{_iso(t.start_time) or ''}|{t.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID]:
    try:
        ts, _, tid = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        return (datetime.fromisoformat(ts) if ts else None, uuid.UUID(tid))
    except (ValueError, UnicodeDecodeError) as exc:
        raise bad_request("invalid cursor") from exc


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def trace_filters(
    q: str | None = None,
    status: Literal["ok", "error"] | None = None,
    tag: list[str] | None = None,
    model: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Any]:
    conds: list[Any] = []
    if q:
        pattern = _like(q[:200])
        conds.append(
            or_(
                Trace.name.ilike(pattern, escape="\\"),
                Trace.input_preview.ilike(pattern, escape="\\"),
                Trace.output_preview.ilike(pattern, escape="\\"),
                Trace.external_id == q,
            )
        )
    if status:
        conds.append(Trace.status == status)
    for t in tag or []:
        conds.append(Trace.tags.contains([t]))
    if model:
        conds.append(Trace.model == model)
    if since:
        conds.append(Trace.start_time >= since)
    if until:
        conds.append(Trace.start_time < until)
    return conds


@router.get("/traces")
async def list_traces(
    project_id: uuid.UUID,
    principal: CurrentUser,
    db: UserDB,
    q: str | None = Query(default=None, max_length=200),
    status: Literal["ok", "error"] | None = None,
    tag: list[str] | None = Query(default=None),
    model: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    await get_project(db, project_id)
    conds = [Trace.project_id == project_id, *trace_filters(q, status, tag, model, since, until)]
    if cursor:
        ts, tid = _decode_cursor(cursor)
        if ts is not None:
            conds.append(or_(Trace.start_time < ts, and_(Trace.start_time == ts, Trace.id < tid)))
    stmt = select(Trace).where(*conds).order_by(Trace.start_time.desc().nulls_last(), Trace.id.desc()).limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return {"traces": [trace_out(t) for t in rows[:limit]], "next_cursor": next_cursor}


@router.get("/traces/facets")
async def trace_facets(project_id: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    await get_project(db, project_id)
    models = (
        (
            await db.execute(
                select(Trace.model)
                .where(Trace.project_id == project_id, Trace.model.is_not(None))
                .distinct()
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    tags = (
        (
            await db.execute(
                select(func.jsonb_array_elements_text(Trace.tags))
                .where(Trace.project_id == project_id)
                .distinct()
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    total = await db.scalar(select(func.count()).select_from(Trace).where(Trace.project_id == project_id))
    return {"models": sorted(m for m in models if m), "tags": sorted(tags), "total": total or 0}


@router.get("/traces/{trace_pk}")
async def read_trace(project_id: uuid.UUID, trace_pk: uuid.UUID, principal: CurrentUser, db: UserDB) -> dict[str, Any]:
    trace = await db.scalar(select(Trace).where(Trace.id == trace_pk, Trace.project_id == project_id))
    if trace is None:
        raise not_found("trace")
    spans = (
        (
            await db.execute(
                select(Span).where(Span.trace_pk == trace.id).order_by(Span.start_time, Span.span_id).limit(5000)
            )
        )
        .scalars()
        .all()
    )
    return {"trace": trace_out(trace), "spans": [span_out(s, project_id) for s in spans]}


@router.get("/spans/{span_pk}/payload/{field}")
async def span_payload(
    project_id: uuid.UUID, span_pk: uuid.UUID, field: Literal["input", "output"], principal: CurrentUser, db: UserDB
) -> Response:
    span = await db.scalar(select(Span).where(Span.id == span_pk, Span.project_id == project_id))
    if span is None:
        raise not_found("span")
    ref = span.input_ref if field == "input" else span.output_ref
    if ref is None:
        value = span.input if field == "input" else span.output
        return Response(json.dumps(value), media_type="application/json")
    if not ref.startswith(f"orgs/{principal.org_id}/"):
        raise not_found("payload")
    try:
        data = await get_store().get(ref)
    except FileNotFoundError as exc:
        raise not_found("payload") from exc
    return Response(data, media_type="application/json")


@router.delete("/traces/{trace_pk}")
async def delete_trace(
    project_id: uuid.UUID, trace_pk: uuid.UUID, principal: CurrentUser, db: UserDB
) -> dict[str, Any]:
    trace = await db.scalar(select(Trace).where(Trace.id == trace_pk, Trace.project_id == project_id))
    if trace is None:
        raise not_found("trace")
    refs = (await db.execute(select(Span.input_ref, Span.output_ref).where(Span.trace_pk == trace.id))).all()
    keys = [k for pair in refs for k in pair if k]
    await db.delete(trace)
    await db.flush()
    await get_store().delete_many(keys)
    await audit.record(
        db,
        principal.org_id,
        "trace.delete",
        user_id=principal.user_id,
        target_type="trace",
        target_id=trace_pk,
        ip=principal.ip,
    )
    return {"ok": True}
