"""ASGI middleware: body size limit, request context/logging/metrics, security headers."""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from replay_api.logs import get_logger

log = get_logger("replay.http")

REQUESTS = Counter("replay_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram(
    "replay_http_request_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


class BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject bodies over ``max_bytes`` (checks Content-Length and streamed bytes)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await _send_413(send)
                        return
                except ValueError:
                    pass
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if not response_started:
                await _send_413(send)


async def _send_413(send: Send) -> None:
    body = b'{"error":{"code":"payload_too_large","message":"request body too large"}}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cache-control", b"no-store"),
]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        incoming = headers.get(b"x-request-id", b"").decode(errors="ignore")[:64]
        request_id = incoming if incoming.replace("-", "").isalnum() and incoming else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status_holder: dict[str, Any] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                hdrs = list(message.get("headers", []))
                existing = {k.lower() for k, _ in hdrs}
                hdrs.append((b"x-request-id", request_id.encode()))
                for k, v in SECURITY_HEADERS:
                    if k not in existing:
                        hdrs.append((k, v))
                if self.hsts:
                    hdrs.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
                message["headers"] = hdrs
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "")
            status = status_holder["status"]
            REQUESTS.labels(method, template, str(status)).inc()
            LATENCY.labels(method, template).observe(elapsed)
            if template not in ("/healthz", "/readyz", "/metrics"):
                log.info("request", method=method, route=template, status=status, ms=round(elapsed * 1000, 1))
