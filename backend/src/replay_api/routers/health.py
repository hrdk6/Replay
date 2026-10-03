"""Liveness, readiness and Prometheus metrics."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from replay_api.config import get_settings
from replay_api.db.session import bare_session
from replay_api.errors import ApiError
from replay_api.logs import get_logger
from replay_api.security.tokens import constant_time_equal
from replay_api.services.storage import get_store

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    s = get_settings()
    return {"status": "ok", "release": s.release}


@router.get("/readyz")
async def readyz() -> JSONResponse:
    checks: dict[str, str] = {}
    ok = True
    try:
        async with asyncio.timeout(3):
            async with bare_session() as db:
                await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        ok = False
        checks["database"] = "error"
        log.warning("readiness_db_failed", error=type(exc).__name__)
    try:
        async with asyncio.timeout(3):
            await get_store().check()
        checks["storage"] = "ok"
    except Exception as exc:
        ok = False
        checks["storage"] = "error"
        log.warning("readiness_storage_failed", error=type(exc).__name__)
    return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    s = get_settings()
    if s.metrics_token is None:
        if s.is_production_like:
            raise ApiError(404, "not_found", "not found")
    else:
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {s.metrics_token.get_secret_value()}"
        if not constant_time_equal(auth, expected):
            raise ApiError(401, "unauthorized", "metrics token required")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
