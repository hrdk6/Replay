"""Alembic environment (async engine; URL from settings / DATABASE_URL)."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from replay_api.config import get_settings
from replay_api.db import models  # noqa: F401  (registers models)
from replay_api.db.base import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        # Data migrations run with RLS bypass for the migration transaction only.
        connection.exec_driver_sql("SELECT set_config('app.system', 'on', true)")
        # Fail fast instead of queueing behind long-running queries during deploys.
        connection.exec_driver_sql("SET LOCAL lock_timeout = '10s'")
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config({"sqlalchemy.url": _url()}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    existing = config.attributes.get("connection")
    if existing is not None:
        do_run_migrations(existing)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
