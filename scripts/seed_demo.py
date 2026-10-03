"""Populate a local Replay with a realistic demo workspace, through the public API.

Requires a running API + worker with DEV_LOGIN_ENABLED=true and the simulator enabled:

    uv run python scripts/seed_demo.py --api http://localhost:8000 --login demo

It signs in as ``--login``, sends ~160 agent traces with the real SDK, builds a dataset,
creates candidates and simulator judges, and runs experiments that come out SAFE,
UNSAFE and with divergence, so every page of the dashboard has something to show.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import time
from typing import Any

import httpx
import replay_sdk as replay

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, api: str, app_origin: str) -> None:
        self.http = httpx.Client(base_url=api, timeout=60)
        self.origin = app_origin
        self.csrf = ""

    def login(self, login: str) -> None:
        r = self.http.post("/api/auth/dev-login", json={"login": login})
        r.raise_for_status()
        self.csrf = self.http.cookies.get("replay_csrf") or ""

    def call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"origin": self.origin}
        if method != "GET":
            headers["x-csrf-token"] = self.csrf
        r = self.http.request(method, path, json=body, headers=headers)
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()


def load_agent() -> Any:
    spec = importlib.util.spec_from_file_location("support_agent", ROOT / "examples" / "support_agent" / "agent.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_for(c: Client, path: str, done: Any, timeout: float = 600) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        obj = c.call("GET", path)
        if done(obj):
            return obj
        time.sleep(1.5)
    raise SystemExit(f"timed out waiting for {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument(
        "--app-origin", default="http://localhost:3000", help="PUBLIC_APP_URL of the API (for the CSRF origin check)"
    )
    ap.add_argument("--login", default="demo")
    ap.add_argument("--traces", type=int, default=160)
    args = ap.parse_args()

    c = Client(args.api, args.app_origin)
    c.login(args.login)
    pid = c.call("GET", "/api/projects")["projects"][0]["id"]
    key = c.call("POST", f"/api/projects/{pid}/api-keys", {"name": "demo seed"})["key"]
    print(f"project {pid}")

    agent = load_agent()
    replay.init(api_key=key, endpoint=args.api, flush_interval=0.2)
    client = replay.wrap_openai(agent.ScriptedLLM())
    questions = agent.QUESTIONS
    for i in range(args.traces):
        q = questions[i % len(questions)]
        route = "/billing" if any(w in q.lower() for w in ("refund", "cancel")) else "/orders"
        agent.answer(client, q, route=route)
    replay.flush(30)
    print(f"sent traces: {replay.stats()}")
    replay.shutdown()

    ds = c.call(
        "POST",
        f"/api/projects/{pid}/datasets",
        {
            "name": "Support questions, October",
            "description": "Random sample of production support traffic",
            "sample_size": 150,
            "seed": 7,
            "filters": {"slice_keys": ["route"]},
        },
    )
    ds = wait_for(c, f"/api/projects/{pid}/datasets/{ds['id']}", lambda d: d["status"] != "building")
    print(f"dataset {ds['id']}: {ds['item_count']} items")

    base = c.call(
        "POST",
        f"/api/projects/{pid}/candidates",
        {"name": "Production (simulated)", "config": {"provider": "simulator", "model": "sim-replay"}},
    )
    same = c.call(
        "POST",
        f"/api/projects/{pid}/candidates",
        {
            "name": "Friendlier system prompt",
            "config": {
                "provider": "simulator",
                "model": "sim-replay",
                "system_prompt": "You are a warm, concise support agent for Acme. Always look up orders before answering.",
            },
        },
    )
    worse = c.call(
        "POST",
        f"/api/projects/{pid}/candidates",
        {
            "name": "Cheaper model (simulated)",
            "config": {"provider": "simulator", "model": "sim-perturb:q=0.55,seed=5"},
        },
    )
    noisy = c.call(
        "POST",
        f"/api/projects/{pid}/candidates",
        {
            "name": "New tool-calling model (simulated)",
            "config": {"provider": "simulator", "model": "sim-perturb:div=0.12,noise=0.2,seed=8"},
        },
    )
    judge = c.call(
        "POST",
        f"/api/projects/{pid}/judges",
        {
            "name": "Answer correctness",
            "mode": "pairwise",
            "provider": "simulator",
            "model": "sim-judge:pb=0.15,noise=0.05",
            "rubric": "The better answer states the correct order status or policy from the tool result,\ndoes not invent details, and is concise and polite.",
        },
    )
    absolute = c.call(
        "POST",
        f"/api/projects/{pid}/judges",
        {
            "name": "Pass/fail check",
            "mode": "absolute",
            "scale": "binary",
            "provider": "simulator",
            "model": "sim-judge",
            "rubric": "Pass if the answer is correct according to the tool results and does not invent details.",
        },
    )

    runs = [
        ("Friendlier system prompt vs production", same["id"], judge["id"], "single_turn"),
        ("Cheaper model vs production", worse["id"], judge["id"], "single_turn"),
        ("New tool-calling model, full agent loop", noisy["id"], absolute["id"], "full_agent"),
    ]
    for name, cand, jid, mode in runs:
        exp = c.call(
            "POST",
            f"/api/projects/{pid}/experiments",
            {
                "name": name,
                "dataset_id": ds["id"],
                "candidate_id": cand,
                "baseline_candidate_id": base["id"],
                "judge_id": jid,
                "mode": mode,
                "budget_usd": 5,
                "settings": {"margin": 5},
            },
        )
        exp = wait_for(
            c, f"/api/projects/{pid}/experiments/{exp['id']}", lambda e: e["status"] not in ("queued", "running")
        )
        print(f"{name}: {exp['status']} {exp.get('verdict')}")

    print(f"\nOpen {args.app_origin}/p/{pid}/experiments (sign in as '{args.login}').")


if __name__ == "__main__":
    sys.exit(main())
