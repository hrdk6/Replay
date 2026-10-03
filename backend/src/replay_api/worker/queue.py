"""Postgres job queue (SELECT ... FOR UPDATE SKIP LOCKED).

Jobs are at-least-once: a handler may run again after a crash or a lost
lock, so every handler must be idempotent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.db.models import Job


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    org_id: uuid.UUID | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def enqueue(
    db: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    *,
    org_id: uuid.UUID | None,
    priority: int = 100,
    delay_seconds: float = 0,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> None:
    """Insert a job in the caller's transaction (so it commits with the change that caused it)."""
    stmt = pg_insert(Job).values(
        org_id=org_id,
        kind=kind,
        payload=payload,
        priority=priority,
        run_after=datetime.now(UTC) + timedelta(seconds=delay_seconds),
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    await db.execute(stmt)


async def claim(db: AsyncSession, worker_id: str) -> ClaimedJob | None:
    row = (
        await db.execute(
            text(
                """
                UPDATE jobs SET status = 'running', locked_at = now(), locked_by = :w, attempts = attempts + 1
                WHERE id = (
                  SELECT id FROM jobs
                  WHERE status = 'queued' AND run_after <= now()
                  ORDER BY priority, run_after, id
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                RETURNING id, org_id, kind, payload, attempts, max_attempts
                """
            ),
            {"w": worker_id},
        )
    ).first()
    if row is None:
        return None
    return ClaimedJob(row.id, row.org_id, row.kind, dict(row.payload or {}), row.attempts, row.max_attempts)


async def complete(db: AsyncSession, job_id: int) -> None:
    # Finished jobs drop their dedupe key so the same work can be queued again later.
    await db.execute(
        text(
            "UPDATE jobs SET status = 'done', finished_at = now(), locked_at = NULL, dedupe_key = NULL WHERE id = :id"
        ),
        {"id": job_id},
    )


async def fail(db: AsyncSession, job: ClaimedJob, error: str) -> str:
    if job.attempts >= job.max_attempts:
        await db.execute(
            text(
                "UPDATE jobs SET status = 'dead', last_error = :e, finished_at = now(), locked_at = NULL, "
                "dedupe_key = NULL WHERE id = :id"
            ),
            {"id": job.id, "e": error[:4000]},
        )
        return "dead"
    backoff = min(600, 5 * 2 ** (job.attempts - 1))
    await db.execute(
        text(
            "UPDATE jobs SET status = 'queued', last_error = :e, locked_at = NULL, locked_by = NULL, "
            "run_after = now() + make_interval(secs => :b) WHERE id = :id"
        ),
        {"id": job.id, "e": error[:4000], "b": backoff},
    )
    return "retry"


async def heartbeat(db: AsyncSession, job_id: int) -> None:
    await db.execute(text("UPDATE jobs SET locked_at = now() WHERE id = :id AND status = 'running'"), {"id": job_id})


async def release(db: AsyncSession, job_id: int) -> None:
    """Return a job to the queue without counting the attempt (graceful shutdown)."""
    await db.execute(
        text(
            "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL, "
            "attempts = GREATEST(attempts - 1, 0) WHERE id = :id AND status = 'running'"
        ),
        {"id": job_id},
    )


async def reap_stale(db: AsyncSession, visibility_timeout_seconds: int) -> int:
    """Requeue jobs whose worker stopped heartbeating."""
    res = await db.execute(
        text(
            "UPDATE jobs SET status = 'queued', locked_at = NULL, locked_by = NULL "
            "WHERE status = 'running' AND locked_at < now() - make_interval(secs => :t) RETURNING id"
        ),
        {"t": visibility_timeout_seconds},
    )
    return len(res.all())


async def try_cron(db: AsyncSession, name: str, interval_seconds: int) -> bool:
    """True for exactly one caller per interval across all workers."""
    await db.execute(
        text("INSERT INTO cron_state (name, last_run_at) VALUES (:n, NULL) ON CONFLICT (name) DO NOTHING"),
        {"n": name},
    )
    res = await db.execute(
        text(
            "UPDATE cron_state SET last_run_at = now() WHERE name = :n AND "
            "(last_run_at IS NULL OR last_run_at < now() - make_interval(secs => :i)) RETURNING name"
        ),
        {"n": name, "i": interval_seconds},
    )
    return res.first() is not None
