"""Usage counters and quota checks (Postgres-backed, per org per UTC day)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from replay_api.db.models import Org, UsageCounter

METRIC_TRACES = "traces"
METRIC_SPANS = "spans"
METRIC_REPLAY_RUNS = "replay_runs"
METRIC_LLM_COST = "llm_cost_usd"
METRIC_REDACTIONS = "redactions"


class QuotaExceeded(Exception):
    def __init__(self, metric: str, limit: float, used: float) -> None:
        super().__init__(f"quota exceeded for {metric}: {used:g} / {limit:g}")
        self.metric = metric
        self.limit = limit
        self.used = used


def today() -> date:
    return datetime.now(UTC).date()


def month_start(d: date | None = None) -> date:
    d = d or today()
    return d.replace(day=1)


async def increment(
    db: AsyncSession, org_id: uuid.UUID, metric: str, amount: float | Decimal, day: date | None = None
) -> None:
    if not amount:
        return
    await db.execute(
        text(
            """
            INSERT INTO usage_counters (org_id, day, metric, value)
            VALUES (:org, :day, :metric, :amount)
            ON CONFLICT (org_id, day, metric) DO UPDATE SET value = usage_counters.value + EXCLUDED.value
            """
        ),
        {"org": org_id, "day": day or today(), "metric": metric, "amount": Decimal(str(amount))},
    )


async def get_value(db: AsyncSession, org_id: uuid.UUID, metric: str, day: date | None = None) -> Decimal:
    row = await db.scalar(
        select(UsageCounter.value).where(
            UsageCounter.org_id == org_id, UsageCounter.day == (day or today()), UsageCounter.metric == metric
        )
    )
    return row or Decimal(0)


async def month_spend(db: AsyncSession, org_id: uuid.UUID) -> Decimal:
    """LLM spend this calendar month (tracked on the month's first day)."""
    return await get_value(db, org_id, METRIC_LLM_COST, month_start())


async def check_daily_quota(db: AsyncSession, org: Org, metric: str, adding: int) -> None:
    limit = org.quota_traces_per_day if metric == METRIC_TRACES else org.quota_replay_runs_per_day
    used = await get_value(db, org.id, metric)
    if used + adding > limit:
        raise QuotaExceeded(metric, float(limit), float(used))


async def reserve_org_spend(db: AsyncSession, org_id: uuid.UUID, amount: Decimal, limit: Decimal) -> bool:
    """Atomically add ``amount`` to this month's spend unless that would exceed ``limit``."""
    await db.execute(
        text(
            """
            INSERT INTO usage_counters (org_id, day, metric, value)
            VALUES (:org, :day, :metric, 0)
            ON CONFLICT (org_id, day, metric) DO NOTHING
            """
        ),
        {"org": org_id, "day": month_start(), "metric": METRIC_LLM_COST},
    )
    res = await db.execute(
        text(
            """
            UPDATE usage_counters SET value = value + :amount
            WHERE org_id = :org AND day = :day AND metric = :metric AND value + :amount <= :limit
            RETURNING value
            """
        ),
        {"org": org_id, "day": month_start(), "metric": METRIC_LLM_COST, "amount": amount, "limit": limit},
    )
    return res.first() is not None


async def adjust_org_spend(db: AsyncSession, org_id: uuid.UUID, delta: Decimal) -> None:
    if delta:
        await increment(db, org_id, METRIC_LLM_COST, delta, month_start())
