"""Engine, sessions, and tenant scoping.

There are exactly two ways to get a database session:

* ``tenant_session(org_id)`` - the default. Every ORM query on a tenant model
  gets ``org_id = :org`` added automatically, inserts for other orgs are
  refused, and Postgres RLS sees ``app.org_id`` for every transaction.
* ``system_session()`` - cross-tenant access for a small, auditable set of
  callers: authentication lookups, the job queue, retention, org deletion.
  Grep for ``system_session(`` to review them all.

A session with neither context refuses to touch tenant models.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, SessionTransaction, with_loader_criteria
from sqlalchemy.orm import Session as OrmSession

from replay_api.config import get_settings
from replay_api.db.base import TenantMixin


class TenantContextError(RuntimeError):
    """Raised when tenant data is touched without an org (or system) context."""


class ScopedOrmSession(OrmSession):
    """Sync session class used under AsyncSession; carries tenant hooks."""


def _is_tenant_mapper(mapper: Any) -> bool:
    return bool(getattr(mapper.class_, "__tenant__", False))


@event.listens_for(ScopedOrmSession, "after_begin")
def _set_rls_context(session: OrmSession, transaction: SessionTransaction, connection: Connection) -> None:
    # Runs at the start of EVERY transaction, so a mid-request commit can't
    # silently drop the RLS context.
    org_id = session.info.get("org_id")
    if org_id is not None:
        connection.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)})
    if session.info.get("system"):
        connection.execute(text("SELECT set_config('app.system', 'on', true)"))


@event.listens_for(ScopedOrmSession, "do_orm_execute")
def _add_tenant_criteria(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    info = state.session.info
    if info.get("system"):
        return
    org_id = info.get("org_id")
    if org_id is None:
        if any(_is_tenant_mapper(m) for m in state.all_mappers):
            raise TenantContextError("tenant model queried without an org context")
        return
    state.statement = state.statement.options(
        with_loader_criteria(TenantMixin, lambda cls: cls.org_id == org_id, include_aliases=True)
    )


@event.listens_for(ScopedOrmSession, "before_flush")
def _check_tenant_writes(session: OrmSession, flush_context: Any, instances: Any) -> None:
    if session.info.get("system"):
        return
    org_id = session.info.get("org_id")
    for obj in list(session.new) + list(session.dirty):
        if not getattr(type(obj), "__tenant__", False):
            continue
        if org_id is None:
            raise TenantContextError(f"writing {type(obj).__name__} without an org context")
        current = getattr(obj, "org_id", None)
        if current is None:
            obj.org_id = org_id
        elif current != org_id:
            raise TenantContextError(f"refusing to write {type(obj).__name__} for another org")


_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=True,
            connect_args={
                "server_settings": {
                    "statement_timeout": str(s.db_statement_timeout_ms),
                    "application_name": "replay",
                }
            },
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), expire_on_commit=False, sync_session_class=ScopedOrmSession)
    return _factory


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None


@asynccontextmanager
async def tenant_session(org_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Session scoped to one org. Commits on success, rolls back on error."""
    if not isinstance(org_id, uuid.UUID):
        raise TypeError("org_id must be a UUID")
    async with get_session_factory()() as session:
        session.info["org_id"] = org_id
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """Cross-tenant session. Use only for auth lookups, queue, retention, org lifecycle."""
    async with get_session_factory()() as session:
        session.info["system"] = True
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def bare_session() -> AsyncIterator[AsyncSession]:
    """Session with no tenant context: may only touch non-tenant tables."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
