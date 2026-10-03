"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from replay_api.config import Settings, get_settings, set_settings
from replay_api.db.session import dispose_engine
from replay_api.errors import install_error_handlers
from replay_api.logs import configure_logging, sentry_before_send
from replay_api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware


def _init_sentry(settings: Settings, component: str) -> None:
    if not settings.sentry_dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        release=settings.release,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        before_send=sentry_before_send,  # type: ignore[arg-type]
    )
    sentry_sdk.set_tag("component", component)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is not None:
        set_settings(settings)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    _init_sentry(settings, "api")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await dispose_engine()

    app = FastAPI(
        title="Replay API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production_like else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production_like else None,
    )
    install_error_handlers(app)

    from replay_api.routers import (
        auth,
        ci,
        datasets,
        experiments,
        health,
        ingest,
        judges,
        orgs,
        projects,
        provider_keys,
        traces,
    )

    for r in (health, auth, orgs, projects, traces, ingest, provider_keys, datasets, judges, experiments, ci):
        app.include_router(r.router)

    # Order: outermost first. No CORS middleware on purpose: the dashboard is
    # same-origin (proxied by Next.js) and the SDK is not a browser client.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production_like)
    return app
