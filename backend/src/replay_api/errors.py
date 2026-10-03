"""Consistent JSON errors: {"error": {"code": str, "message": str, "details"?: ...}}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from replay_api.db.session import TenantContextError
from replay_api.logs import get_logger
from replay_api.services.usage import QuotaExceeded

log = get_logger(__name__)


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def not_found(what: str = "resource") -> ApiError:
    # Cross-tenant lookups deliberately get the same 404 as missing rows.
    return ApiError(404, "not_found", f"{what} not found")


def bad_request(message: str, details: Any = None) -> ApiError:
    return ApiError(400, "bad_request", message, details)


def forbidden(message: str = "insufficient permissions") -> ApiError:
    return ApiError(403, "forbidden", message)


def conflict(message: str) -> ApiError:
    return ApiError(409, "conflict", message)


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo submitted values back: they may contain secrets (e.g. a provider key).
        details = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()[:50]
        ]
        return JSONResponse(_body("validation_error", "request validation failed", details), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            _body(code, str(exc.detail)), status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(QuotaExceeded)
    async def _quota(_: Request, exc: QuotaExceeded) -> JSONResponse:
        return JSONResponse(
            _body("quota_exceeded", str(exc), {"metric": exc.metric, "limit": exc.limit, "used": exc.used}),
            status_code=429,
        )

    @app.exception_handler(TenantContextError)
    async def _tenant(_: Request, exc: TenantContextError) -> JSONResponse:
        log.error("tenant_context_violation", error=str(exc))
        return JSONResponse(_body("internal_error", "internal error"), status_code=500)

    @app.exception_handler(RecursionError)
    async def _recursion(_: Request, __: RecursionError) -> JSONResponse:
        return JSONResponse(_body("bad_request", "payload nested too deeply"), status_code=400)
