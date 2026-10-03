"""In-process token-bucket rate limiter.

Limits are per process: with N API instances the effective burst is N times
higher. That is acceptable as abuse protection; hard per-org quotas
(traces/day, runs/day, spend) are enforced in Postgres (services.quotas).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, max_keys: int = 50_000) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._max_keys = max_keys

    def hit(self, key: str, per_minute: int, cost: float = 1.0, now: float | None = None) -> float:
        """Consume ``cost`` tokens. Returns 0 if allowed, else seconds to wait."""
        if per_minute <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        rate = per_minute / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._evict(now, rate)
            bucket = _Bucket(float(per_minute), now)
            self._buckets[key] = bucket
        bucket.tokens = min(float(per_minute), bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return 0.0
        return math.ceil((cost - bucket.tokens) / rate * 10) / 10

    def _evict(self, now: float, rate: float) -> None:
        # Drop buckets that would be full again (idle long enough).
        stale = [k for k, b in self._buckets.items() if now - b.updated > 120]
        for k in stale[: max(1, len(stale))]:
            self._buckets.pop(k, None)
        if len(self._buckets) >= self._max_keys:
            self._buckets.clear()

    def reset(self) -> None:
        self._buckets.clear()


limiter = RateLimiter()
