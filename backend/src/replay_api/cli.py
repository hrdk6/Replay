"""Process entry points: ``replay-api`` (HTTP server) and ``replay-admin`` (operator tasks)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "replay_api.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - container listens on all interfaces
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
        workers=int(os.environ.get("WEB_CONCURRENCY", "2")),
        log_config=None,
        server_header=False,
    )


async def _set_quota(slug: str, traces: int | None, runs: int | None, budget: float | None) -> None:
    from sqlalchemy import select

    from replay_api.db.models import Org
    from replay_api.db.session import dispose_engine, system_session

    async with system_session() as db:
        org = await db.scalar(select(Org).where(Org.slug == slug))
        if org is None:
            print(f"no org with slug {slug!r}", file=sys.stderr)
            raise SystemExit(1)
        if traces is not None:
            org.quota_traces_per_day = traces
        if runs is not None:
            org.quota_replay_runs_per_day = runs
        if budget is not None:
            org.monthly_budget_usd = Decimal(str(budget))
        print(
            f"{org.slug}: traces/day={org.quota_traces_per_day} runs/day={org.quota_replay_runs_per_day} "
            f"budget=${org.monthly_budget_usd}"
        )
    await dispose_engine()


def admin() -> None:
    parser = argparse.ArgumentParser(prog="replay-admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    q = sub.add_parser("set-quota", help="change an organization's quotas")
    q.add_argument("slug")
    q.add_argument("--traces-per-day", type=int)
    q.add_argument("--runs-per-day", type=int)
    q.add_argument("--monthly-budget-usd", type=float)
    args = parser.parse_args()
    if args.cmd == "migrate":
        from alembic import command
        from alembic.config import Config

        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        command.upgrade(Config(os.path.join(here, "alembic.ini")), "head")
    elif args.cmd == "set-quota":
        asyncio.run(_set_quota(args.slug, args.traces_per_day, args.runs_per_day, args.monthly_budget_usd))
