"""Worker process: claims jobs from Postgres and runs them with bounded concurrency."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
import traceback
import uuid

from sqlalchemy import text

from replay_api.config import Settings, get_settings
from replay_api.db.session import dispose_engine, system_session
from replay_api.logs import configure_logging, get_logger
from replay_api.services.storage import PayloadStore, get_store
from replay_api.worker import queue
from replay_api.worker.handlers import HANDLERS, Reschedule

log = get_logger("replay.worker")


class Worker:
    def __init__(self, settings: Settings, store: PayloadStore | None = None, concurrency: int | None = None) -> None:
        self.settings = settings
        self.store = store or get_store()
        self.concurrency = concurrency or settings.worker_concurrency
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop = asyncio.Event()
        self._tasks: dict[asyncio.Task[None], int] = {}
        self._last_maintenance = 0.0

    async def _maintenance(self) -> None:
        if time.monotonic() - self._last_maintenance < 30:
            return
        self._last_maintenance = time.monotonic()
        async with system_session() as db:
            reaped = await queue.reap_stale(db, self.settings.job_visibility_timeout_seconds)
            if reaped:
                log.warning("requeued_stale_jobs", count=reaped)
            if await queue.try_cron(db, "retention.sweep", self.settings.retention_sweep_interval_seconds):
                await queue.enqueue(db, "retention.sweep", {}, org_id=None, priority=200, dedupe_key="retention.sweep")

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(60)
            with contextlib.suppress(Exception):
                async with system_session() as db:
                    await queue.heartbeat(db, job_id)

    async def run_job(self, job: queue.ClaimedJob) -> None:
        handler = HANDLERS.get(job.kind)
        hb = asyncio.create_task(self._heartbeat(job.id))
        start = time.perf_counter()
        try:
            if handler is None:
                raise RuntimeError(f"no handler for job kind {job.kind!r}")
            await handler(job, self.settings, self.store)
        except Reschedule as r:
            async with system_session() as db:
                await queue.release(db, job.id)
                await db.execute(
                    text("UPDATE jobs SET run_after = now() + make_interval(secs => :d) WHERE id = :id"),
                    {"d": r.delay, "id": job.id},
                )
            return
        except asyncio.CancelledError:
            async with system_session() as db:
                await queue.release(db, job.id)
            raise
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
            async with system_session() as db:
                outcome = await queue.fail(db, job, err)
            log.error("job_failed", job_id=job.id, kind=job.kind, outcome=outcome, error=f"{type(exc).__name__}: {exc}")
            if self.settings.sentry_dsn:
                import sentry_sdk

                sentry_sdk.capture_exception(exc)
            return
        finally:
            hb.cancel()
        async with system_session() as db:
            await queue.complete(db, job.id)
        log.info("job_done", job_id=job.id, kind=job.kind, ms=round((time.perf_counter() - start) * 1000))

    async def run(self, drain: bool = False) -> None:
        """Main loop. With ``drain=True`` it exits once the queue is empty (tests, one-off runs)."""
        log.info("worker_started", worker_id=self.worker_id, concurrency=self.concurrency)
        while not self.stop.is_set():
            try:
                await self._maintenance()
            except Exception as exc:
                log.warning("maintenance_failed", error=str(exc))
            if len(self._tasks) >= self.concurrency:
                done, _ = await asyncio.wait(
                    self._tasks, timeout=self.settings.worker_poll_interval_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    self._tasks.pop(t, None)
                continue
            async with system_session() as db:
                job = await queue.claim(db, self.worker_id)
            if job is None:
                if drain and not self._tasks and not await self._has_future_work():
                    break
                with contextlib.suppress(TimeoutError):
                    if self._tasks:
                        done, _ = await asyncio.wait(
                            self._tasks,
                            timeout=self.settings.worker_poll_interval_seconds,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in done:
                            self._tasks.pop(t, None)
                    else:
                        await asyncio.wait_for(self.stop.wait(), self.settings.worker_poll_interval_seconds)
                continue
            task = asyncio.create_task(self.run_job(job))
            self._tasks[task] = job.id
        await self._shutdown()

    async def _has_future_work(self) -> bool:
        async with system_session() as db:
            n = await db.scalar(text("SELECT count(*) FROM jobs WHERE status IN ('queued','running')"))
        return bool(n)

    async def _shutdown(self) -> None:
        if not self._tasks:
            return
        log.info("worker_draining", running=len(self._tasks))
        _, pending = await asyncio.wait(self._tasks, timeout=30)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.sentry_dsn:
        from replay_api.main import _init_sentry

        _init_sentry(settings, "worker")
    worker = Worker(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop.set)
    try:
        await worker.run()
    finally:
        await dispose_engine()


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()
