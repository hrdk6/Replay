"""Ingest load test: concurrent batched POST /v1/ingest, reporting throughput and latency.

    REPLAY_API_KEY=rk_... uv run python scripts/load_test.py --api http://localhost:8000 --concurrency 16 --seconds 30

Each request carries --traces-per-batch synthetic agent traces (4 spans each). Point it at
staging, never production, and mind the org's daily trace quota.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


def batch(n_traces: int) -> dict[str, Any]:
    spans: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    t0 = datetime.now(UTC)
    for _ in range(n_traces):
        tid = f"load-{uuid.uuid4().hex}"
        traces.append({"trace_id": tid, "tags": ["load-test"], "metadata": {"route": "/load"}})
        msgs = [
            {"role": "system", "content": "You are a support agent."},
            {"role": "user", "content": "Where is order A100? " * 5},
        ]
        spans += [
            {
                "trace_id": tid,
                "span_id": "root",
                "name": "agent",
                "kind": "agent",
                "start_time": t0.isoformat(),
                "end_time": (t0 + timedelta(seconds=2)).isoformat(),
                "input": {"q": "Where is order A100?"},
                "output": "Shipped.",
            },
            {
                "trace_id": tid,
                "span_id": "llm1",
                "parent_span_id": "root",
                "name": "chat",
                "kind": "llm",
                "start_time": t0.isoformat(),
                "attributes": {
                    "gen_ai.request.model": "gpt-4o-mini",
                    "gen_ai.usage.input_tokens": 120,
                    "gen_ai.usage.output_tokens": 20,
                },
                "input": {"messages": msgs},
                "output": {
                    "role": "assistant",
                    "tool_calls": [{"id": "c", "name": "get_order", "arguments": {"order_id": "A100"}}],
                },
            },
            {
                "trace_id": tid,
                "span_id": "tool1",
                "parent_span_id": "root",
                "name": "get_order",
                "kind": "tool",
                "start_time": t0.isoformat(),
                "input": {"name": "get_order", "arguments": {"order_id": "A100"}},
                "output": {"status": "shipped"},
            },
            {
                "trace_id": tid,
                "span_id": "llm2",
                "parent_span_id": "root",
                "name": "chat",
                "kind": "llm",
                "start_time": t0.isoformat(),
                "attributes": {
                    "gen_ai.request.model": "gpt-4o-mini",
                    "gen_ai.usage.input_tokens": 160,
                    "gen_ai.usage.output_tokens": 12,
                },
                "input": {"messages": msgs},
                "output": {"role": "assistant", "content": "Your order shipped."},
            },
        ]
    return {"spans": spans, "traces": traces}


async def worker(
    client: httpx.AsyncClient, deadline: float, n: int, latencies: list[float], errors: dict[str, int]
) -> None:
    while time.perf_counter() < deadline:
        body = batch(n)
        start = time.perf_counter()
        try:
            r = await client.post("/v1/ingest", json=body)
            key = str(r.status_code)
        except httpx.HTTPError as exc:
            key = type(exc).__name__
        latencies.append(time.perf_counter() - start)
        if key != "202":
            errors[key] = errors.get(key, 0) + 1


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--traces-per-batch", type=int, default=10)
    args = ap.parse_args()
    key = os.environ.get("REPLAY_API_KEY")
    if not key:
        sys.exit("set REPLAY_API_KEY")
    latencies: list[float] = []
    errors: dict[str, int] = {}
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(
        base_url=args.api, headers={"Authorization": f"Bearer {key}"}, timeout=30, limits=limits
    ) as client:
        deadline = time.perf_counter() + args.seconds
        t0 = time.perf_counter()
        await asyncio.gather(
            *(worker(client, deadline, args.traces_per_batch, latencies, errors) for _ in range(args.concurrency))
        )
        elapsed = time.perf_counter() - t0
    ok = len(latencies) - sum(errors.values())
    q = statistics.quantiles(latencies, n=100) if len(latencies) >= 2 else [0.0] * 99
    print(
        f"requests: {len(latencies)} in {elapsed:.1f}s ({len(latencies) / elapsed:.1f} req/s), errors: {errors or 'none'}"
    )
    print(
        f"spans/s accepted: {ok * args.traces_per_batch * 4 / elapsed:.0f}   traces/s: {ok * args.traces_per_batch / elapsed:.0f}"
    )
    print(f"latency p50 {q[49] * 1000:.0f} ms   p95 {q[94] * 1000:.0f} ms   p99 {q[98] * 1000:.0f} ms")


if __name__ == "__main__":
    asyncio.run(main())
