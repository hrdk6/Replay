"""SDK-facing ingest endpoints (API-key auth)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from replay_api.config import get_settings
from replay_api.db.models import Trace
from replay_api.deps import CurrentKey, KeyDB, get_org, get_project
from replay_api.errors import ApiError
from replay_api.routers.traces import trace_out
from replay_api.services.ingest import IngestRequest, ingest_batch
from replay_api.services.otlp import convert_otlp, empty_protobuf_response, parse_protobuf
from replay_api.services.storage import get_store

router = APIRouter(tags=["ingest"])


def _validation_error(exc: ValidationError) -> ApiError:
    details = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()[:50]]
    return ApiError(422, "validation_error", "request validation failed", details)


async def _read_json(request: Request) -> Any:
    body = await request.body()
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiError(400, "bad_request", "body is not valid JSON") from exc


@router.post("/v1/ingest", status_code=202)
async def ingest(request: Request, principal: CurrentKey, db: KeyDB) -> dict[str, Any]:
    settings = get_settings()
    raw = await _read_json(request)
    try:
        req = IngestRequest.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    if len(req.spans) > settings.max_spans_per_batch:
        raise ApiError(413, "too_many_spans", f"at most {settings.max_spans_per_batch} spans per request")
    org = await get_org(db, principal.org_id)
    project = await get_project(db, principal.project_id)
    result = await ingest_batch(db, get_store(), settings, org, project, req)
    return result.model_dump()


@router.post("/v1/otlp/v1/traces")
async def otlp_traces(request: Request, principal: CurrentKey, db: KeyDB) -> Response:
    """OTLP/HTTP traces. Point an OTLP exporter at ``<api>/v1/otlp`` with the API key header."""
    settings = get_settings()
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    body = await request.body()
    from_proto = ctype == "application/x-protobuf"
    try:
        payload = parse_protobuf(body) if from_proto else json.loads(body)
    except Exception as exc:
        raise ApiError(400, "bad_request", "could not parse OTLP payload") from exc
    try:
        req = convert_otlp(payload, from_proto=from_proto)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    if len(req.spans) > settings.max_spans_per_batch * 5:
        raise ApiError(413, "too_many_spans", "OTLP batch too large")
    org = await get_org(db, principal.org_id)
    project = await get_project(db, principal.project_id)
    await ingest_batch(db, get_store(), settings, org, project, req)
    if from_proto:
        return Response(empty_protobuf_response(), media_type="application/x-protobuf")
    return JSONResponse({})


@router.get("/v1/traces/{trace_id}")
async def read_trace_by_id(trace_id: str, principal: CurrentKey, db: KeyDB) -> dict[str, Any]:
    """Look up a trace by the id your application sent (used by smoke tests and SDK users)."""
    trace = await db.scalar(
        select(Trace).where(Trace.project_id == principal.project_id, Trace.external_id == trace_id[:128])
    )
    if trace is None:
        raise ApiError(404, "not_found", "trace not found")
    return trace_out(trace)


@router.get("/v1/project")
async def whoami(principal: CurrentKey, db: KeyDB) -> dict[str, Any]:
    project = await get_project(db, principal.project_id)
    return {"project_id": str(project.id), "project_name": project.name, "org_id": str(principal.org_id)}
