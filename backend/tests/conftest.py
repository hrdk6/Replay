"""Test fixtures: a real Postgres (non-superuser role, RLS active), in-memory object store.

Requires Postgres from docker-compose (``docker compose up -d postgres``) or
``TEST_DATABASE_URL`` pointing at a database owned by a NON-superuser role.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from replay_api.config import Settings, set_settings
from replay_api.db.session import dispose_engine
from replay_api.security.ratelimit import limiter
from replay_api.services.storage import MemoryPayloadStore, set_store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://replay:replay@localhost:5432/replay_test")
APP_URL = "http://testserver"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "database_url": TEST_DATABASE_URL,
        "storage_backend": "memory",
        "dev_login_enabled": True,
        "signup_mode": "open",
        "public_app_url": APP_URL,
        "public_api_url": APP_URL,
        "log_json": False,
        "log_level": "WARNING",
        "rate_limit_api_per_minute": 100_000,
        "rate_limit_ingest_per_minute": 100_000,
        "rate_limit_auth_per_minute": 100_000,
        "worker_poll_interval_seconds": 0.05,
        "enable_simulator_provider": True,
    }
    base.update(overrides)
    return Settings(**base)


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _migrated_db() -> None:
    set_settings(make_settings())
    _run_migrations()


TABLES = [
    "human_labels",
    "judge_calibrations",
    "judge_results",
    "experiment_runs",
    "experiments",
    "judges",
    "candidates",
    "dataset_items",
    "datasets",
    "spans",
    "traces",
    "api_keys",
    "provider_keys",
    "audit_log",
    "usage_counters",
    "invites",
    "memberships",
    "projects",
    "sessions",
    "jobs",
    "cron_state",
    "users",
    "orgs",
]


async def truncate_all() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    set_settings(make_settings())
    limiter.reset()
    if request.node.get_closest_marker("no_clean") is None:
        set_store(MemoryPayloadStore())
        await truncate_all()
    yield
    await dispose_engine()


@pytest.fixture
def store() -> MemoryPayloadStore:
    from replay_api.services.storage import get_store

    s = get_store()
    assert isinstance(s, MemoryPayloadStore)
    return s


def build_app() -> Any:
    from replay_api.main import create_app

    return create_app()


@dataclass
class UserClient:
    """An httpx client signed in as one dashboard user (cookies + CSRF handled)."""

    client: httpx.AsyncClient
    login: str
    csrf: str = ""
    org_id: str = ""
    user_id: str = ""

    def _headers(self, method: str, extra: dict[str, str] | None) -> dict[str, str]:
        h = {"origin": APP_URL}
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            h["x-csrf-token"] = self.csrf
        h.update(extra or {})
        return h

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        headers = self._headers(method, kw.pop("headers", None))
        return await self.client.request(method, url, headers=headers, **kw)

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("POST", url, **kw)

    async def patch(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kw)

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kw)


async def login_client(app: Any, login: str) -> UserClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=APP_URL)
    r = await client.post("/api/auth/dev-login", json={"login": login})
    assert r.status_code == 200, r.text
    uc = UserClient(client, login, csrf=client.cookies.get("replay_csrf") or "")
    me = (await uc.get("/api/me")).json()
    uc.org_id = me["active_org_id"]
    uc.user_id = me["user"]["id"]
    return uc


@pytest.fixture
async def app() -> Any:
    return build_app()


@pytest.fixture
async def alice(app: Any) -> AsyncIterator[UserClient]:
    uc = await login_client(app, "alice")
    yield uc
    await uc.client.aclose()


@pytest.fixture
async def bob(app: Any) -> AsyncIterator[UserClient]:
    uc = await login_client(app, "bob")
    yield uc
    await uc.client.aclose()


async def first_project(uc: UserClient) -> str:
    r = await uc.get("/api/projects")
    return str(r.json()["projects"][0]["id"])


async def make_api_key(uc: UserClient, project_id: str) -> str:
    r = await uc.post(f"/api/projects/{project_id}/api-keys", json={"name": "test"})
    assert r.status_code == 201, r.text
    return str(r.json()["key"])


def key_client(app: Any, key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=APP_URL, headers={"authorization": f"Bearer {key}"}
    )


async def drain_worker() -> None:
    from replay_api.config import get_settings
    from replay_api.services.storage import get_store
    from replay_api.worker.main import Worker

    await Worker(get_settings(), get_store(), concurrency=4).run(drain=True)
