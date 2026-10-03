"""Phase 0.5 divergence spike (research code).

Question: when a recorded agent run is replayed with a *different* model, how
often do its tool calls fail to match the recording, and how often does a
fuzzy match silently return the WRONG recorded result?

Method
------
* Four deterministic local tools (weather, flights, help-center search, order
  lookup) with realistic argument canonicalisation (city aliases, date formats,
  case-insensitive ids, schema defaults).
* Model A runs each task live -> recording. Model B runs the same task live
  (true tool results are fed back, so the trajectory is what B would really do).
  At every tool call of B we ask the matcher, at several settings, what replay
  would have returned, and compare with the TRUE result for B's arguments.
  That gives, per setting: divergence rate and false-match (wrong result) rate,
  from a single pass.
* Models: ``synthetic`` (a model pair whose phrasing differences follow
  documented probabilities - an assumption, not data) or real models via the
  official SDKs when OPENAI_API_KEY / ANTHROPIC_API_KEY are set.

    uv run python spike/divergence/spike.py                       # synthetic
    uv run python spike/divergence/spike.py --model-a gpt-4o-mini --model-b claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from replay_api.replay.matching import ToolEvent, ToolMatcher  # noqa: E402

# --- Tools (ground truth) -----------------------------------------------------------------

CITY_ALIASES = {
    "nyc": "new york",
    "new york city": "new york",
    "ny": "new york",
    "sf": "san francisco",
    "san fran": "san francisco",
    "la": "los angeles",
    "l.a.": "los angeles",
}
CITIES = ["new york", "san francisco", "los angeles", "boston", "austin", "seattle", "chicago", "denver"]
AIRPORTS = {
    "new york": "JFK",
    "san francisco": "SFO",
    "los angeles": "LAX",
    "boston": "BOS",
    "austin": "AUS",
    "seattle": "SEA",
    "chicago": "ORD",
    "denver": "DEN",
}
KB = {
    "refund": "Refunds go to the original payment method within 5-7 business days.",
    "baggage": "One carry-on and one personal item are free; checked bags cost $35.",
    "cancel": "Bookings can be cancelled free within 24 hours.",
    "pets": "Small pets under 8kg may travel in the cabin.",
}
ORDERS = {"A100": "shipped", "B200": "processing", "C300": "delivered", "D400": "cancelled"}

TOOL_SCHEMAS = [
    {
        "name": "get_weather",
        "description": "Weather forecast for a city on a date",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["city", "date"],
        },
    },
    {
        "name": "search_flights",
        "description": "Find flights",
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
                "date": {"type": "string"},
                "cabin": {"type": "string", "default": "economy"},
            },
            "required": ["origin", "destination", "date"],
        },
    },
    {
        "name": "search_kb",
        "description": "Search the help center",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 3}},
            "required": ["query"],
        },
    },
    {
        "name": "get_order",
        "description": "Look up an order",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
]


def canon_city(c: Any) -> str:
    s = re.sub(r"[^a-z. ]", "", str(c).strip().lower())
    return CITY_ALIASES.get(s, s)


def canon_date(d: Any) -> str:
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def run_tool(name: str, args: dict[str, Any]) -> Any:
    if name == "get_weather":
        city, d = canon_city(args.get("city", "")), canon_date(args.get("date", ""))
        seed = sum(map(ord, city + d))
        return {"city": city, "date": d, "high_c": 10 + seed % 20, "rain": seed % 3 == 0}
    if name == "search_flights":
        o, dst, d = (
            canon_city(args.get("origin", "")),
            canon_city(args.get("destination", "")),
            canon_date(args.get("date", "")),
        )
        cabin = str(args.get("cabin", "economy")).lower()
        seed = sum(map(ord, o + dst + d + cabin))
        return {
            "route": f"{AIRPORTS.get(o, '???')}-{AIRPORTS.get(dst, '???')}",
            "date": d,
            "cabin": cabin,
            "price": 120 + seed % 400,
        }
    if name == "search_kb":
        q = str(args.get("query", "")).lower()
        k = int(args.get("top_k", 3))
        return [text for key, text in KB.items() if key in q][:k] or ["No article found."]
    if name == "get_order":
        oid = str(args.get("order_id", "")).upper().strip()
        return {"order_id": oid, "status": ORDERS.get(oid, "not found")}
    return {"error": f"unknown tool {name}"}


# --- Tasks ------------------------------------------------------------------------------


@dataclass
class Task:
    prompt: str
    calls: list[tuple[str, dict[str, Any]]]  # canonical intended calls (synthetic models use these)


def make_tasks(n: int, rng: random.Random) -> list[Task]:
    tasks = []
    for i in range(n):
        kind = i % 4
        d = date(2026, 10, 1 + (i % 25)).isoformat()
        a, b = rng.sample(CITIES, 2)
        if kind == 0:
            tasks.append(Task(f"What's the weather in {a.title()} on {d}?", [("get_weather", {"city": a, "date": d})]))
        elif kind == 1:
            tasks.append(
                Task(
                    f"Find me a flight from {a.title()} to {b.title()} on {d}, and tell me the weather there.",
                    [
                        ("search_flights", {"origin": a, "destination": b, "date": d}),
                        ("get_weather", {"city": b, "date": d}),
                    ],
                )
            )
        elif kind == 2:
            topic = rng.choice(list(KB))
            tasks.append(Task(f"What is your {topic} policy?", [("search_kb", {"query": f"{topic} policy"})]))
        else:
            oid = rng.choice(list(ORDERS))
            topic = rng.choice(["refund", "cancel"])
            tasks.append(
                Task(
                    f"My order {oid} - what's its status, and what's the {topic} policy?",
                    [("get_order", {"order_id": oid}), ("search_kb", {"query": f"{topic} policy"})],
                )
            )
    return tasks


# --- Models -------------------------------------------------------------------------------

ToolCall = tuple[str, dict[str, Any]]


class SyntheticModel:
    """Issues the task's intended calls, re-phrased according to ``style``.

    The probabilities below are ASSUMPTIONS chosen to resemble differences seen
    between model families (aliases, date formats, optional args, extra words,
    occasional different tool plans, rare genuine misreads). They are not data.
    """

    STYLES: dict[str, dict[str, float]] = {
        "canonical": {},
        "other_family": {
            "alias": 0.35,
            "date_format": 0.3,
            "case": 0.3,
            "extra_words": 0.35,
            "optional_default": 0.25,
            "optional_other": 0.05,
            "reorder": 0.15,
            "extra_call": 0.08,
            "misread": 0.03,
            "near_date": 0.03,
        },
    }

    def __init__(self, style: str, seed: int) -> None:
        self.p = self.STYLES[style]
        self.seed = seed

    def plan(self, task_idx: int, task: Task) -> list[ToolCall]:
        rng = random.Random(f"{self.seed}-{task_idx}")
        p = self.p
        calls: list[ToolCall] = []
        for name, args in task.calls:
            a = dict(args)
            for key in ("city", "origin", "destination"):
                if key in a:
                    if rng.random() < p.get("misread", 0):
                        a[key] = rng.choice([c for c in CITIES if c != a[key]])  # genuinely different call
                    elif rng.random() < p.get("alias", 0):
                        inv = {v: k for k, v in CITY_ALIASES.items()}
                        a[key] = inv.get(a[key], a[key])
                    if rng.random() < p.get("case", 0):
                        a[key] = a[key].title()
            if "date" in a:
                if rng.random() < p.get("near_date", 0):
                    a["date"] = (
                        date.fromisoformat(a["date"]).replace(day=min(28, date.fromisoformat(a["date"]).day + 1))
                    ).isoformat()
                elif rng.random() < p.get("date_format", 0):
                    a["date"] = datetime.fromisoformat(a["date"]).strftime("%b %d, %Y")
            if "query" in a and rng.random() < p.get("extra_words", 0):
                a["query"] = rng.choice(
                    [f"{a['query']} details", f"what is the {a['query']}", f"{a['query']} for customers"]
                )
            if name == "search_flights":
                if rng.random() < p.get("optional_default", 0):
                    a["cabin"] = "economy"
                elif rng.random() < p.get("optional_other", 0):
                    a["cabin"] = "business"
            if name == "search_kb" and rng.random() < p.get("optional_default", 0):
                a["top_k"] = 3
            if "order_id" in a and rng.random() < p.get("case", 0):
                a["order_id"] = a["order_id"].lower()
            calls.append((name, a))
        if len(calls) > 1 and rng.random() < p.get("reorder", 0):
            calls.reverse()
        if rng.random() < p.get("extra_call", 0):
            calls.append(("search_kb", {"query": "general help"}))
        return calls


class RealModel:
    """Runs a real agent loop via the official SDKs (OpenAI or Anthropic)."""

    def __init__(self, model: str) -> None:
        self.model = model
        if model.startswith("claude"):
            from anthropic import Anthropic

            self.client: Any = Anthropic()
            self.kind = "anthropic"
        else:
            from openai import OpenAI

            self.client = OpenAI()
            self.kind = "openai"

    def run(self, task: Task, execute: Callable[[str, dict[str, Any]], Any], max_steps: int = 6) -> list[ToolCall]:
        system = "You are a travel and support assistant. Use the tools to answer. Today is 2026-09-30."
        calls: list[ToolCall] = []
        if self.kind == "openai":
            tools = [{"type": "function", "function": t} for t in TOOL_SCHEMAS]
            msgs: list[dict[str, Any]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": task.prompt},
            ]
            for _ in range(max_steps):
                r = self.client.chat.completions.create(model=self.model, messages=msgs, tools=tools)
                m = r.choices[0].message
                if not m.tool_calls:
                    break
                msgs.append(
                    {"role": "assistant", "content": m.content, "tool_calls": [tc.model_dump() for tc in m.tool_calls]}
                )
                for tc in m.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    calls.append((tc.function.name, args))
                    msgs.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(execute(tc.function.name, args))}
                    )
            return calls
        tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOL_SCHEMAS
        ]
        amsgs: list[dict[str, Any]] = [{"role": "user", "content": task.prompt}]
        for _ in range(max_steps):
            r = self.client.messages.create(
                model=self.model, system=system, max_tokens=2048, tools=tools, messages=amsgs
            )
            uses = [b for b in r.content if b.type == "tool_use"]
            if not uses:
                break
            amsgs.append({"role": "assistant", "content": [b.model_dump() for b in r.content]})
            results = []
            for b in uses:
                args = dict(b.input)
                calls.append((b.name, args))
                results.append(
                    {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(execute(b.name, args))}
                )
            amsgs.append({"role": "user", "content": results})
        return calls


# --- Matching variants under test -------------------------------------------------------------


@dataclass
class Setting:
    name: str
    threshold: float  # 1.01 => exact only
    allow_reuse: bool = True
    typed: bool = False  # exact equality required for id/date/number leaves
    schema_defaults: bool = False


SETTINGS = [
    Setting("v1: untyped exact only", 1.01),
    Setting("v1: untyped fuzzy 0.95", 0.95),
    Setting("v1: untyped fuzzy 0.85", 0.85),
    Setting("v1: untyped fuzzy 0.70", 0.70),
    Setting("v2: typed exact only + defaults", 1.01, typed=True, schema_defaults=True),
    Setting("v2: typed fuzzy 0.85", 0.85, typed=True),
    Setting("v2: typed fuzzy 0.85 + defaults", 0.85, typed=True, schema_defaults=True),
    Setting("v2: typed fuzzy 0.70 + defaults", 0.70, typed=True, schema_defaults=True),
    Setting("v2: typed fuzzy 0.60 + defaults", 0.60, typed=True, schema_defaults=True),
]


def make_matcher(events: list[ToolEvent], s: Setting) -> Any:
    return ToolMatcher(
        events,
        threshold=s.threshold,
        allow_reuse=s.allow_reuse,
        typed=s.typed,
        tool_schemas=TOOL_SCHEMAS if s.schema_defaults else None,
    )


@dataclass
class Tally:
    runs: int = 0
    diverged_runs: int = 0
    calls: int = 0
    matched: int = 0
    false_matches: int = 0
    silent_wrong_runs: int = 0  # runs with a wrong result and no divergence: the dangerous case
    reasons: Counter[str] = field(default_factory=Counter)


def evaluate(tasks: list[Task], rec_calls: list[list[ToolCall]], rep_calls: list[list[ToolCall]]) -> dict[str, Tally]:
    out: dict[str, Tally] = {}
    for s in SETTINGS:
        t = Tally()
        for recorded, replayed in zip(rec_calls, rep_calls, strict=True):
            events = [ToolEvent(i, "tool", n, a, run_tool(n, a)) for i, (n, a) in enumerate(recorded)]
            matcher = make_matcher(events, s)
            if matcher is None:
                break
            t.runs += 1
            diverged = wrong = False
            for name, args in replayed:
                t.calls += 1
                m = matcher.match(name, args)
                if m.event is None:
                    diverged = True
                    t.reasons["no match: " + ("unknown tool" if m.best_score == 0 else "args differ")] += 1
                    break  # replay stops at the first divergence
                t.matched += 1
                if json.dumps(m.event.result, sort_keys=True) != json.dumps(run_tool(name, args), sort_keys=True):
                    t.false_matches += 1
                    wrong = True
            t.diverged_runs += diverged
            t.silent_wrong_runs += wrong and not diverged
        if t.runs:
            out[s.name] = t
    return out


def report(results: dict[str, Tally], label: str) -> str:
    lines = [
        f"### {label}",
        "",
        "| Matching | Divergent runs | Wrong results (of matched calls) | Silently wrong runs |",
        "|---|---|---|---|",
    ]
    for name, t in results.items():
        lines.append(
            f"| {name} | {t.diverged_runs}/{t.runs} ({t.diverged_runs / t.runs:.0%}) | "
            f"{t.false_matches}/{t.matched} ({t.false_matches / max(t.matched, 1):.1%}) | "
            f"{t.silent_wrong_runs}/{t.runs} ({t.silent_wrong_runs / t.runs:.1%}) |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model-a", default="synthetic:canonical")
    ap.add_argument("--model-b", default="synthetic:other_family")
    ap.add_argument("--out", default=str(Path(__file__).parent / "out"))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    tasks = make_tasks(args.tasks, rng)

    def calls_for(model: str) -> list[list[ToolCall]]:
        if model.startswith("synthetic:"):
            m = SyntheticModel(model.split(":", 1)[1], args.seed)
            return [m.plan(i, t) for i, t in enumerate(tasks)]
        rm = RealModel(model)
        return [rm.run(t, run_tool) for t in tasks]

    rec = calls_for(args.model_a)
    rep = calls_for(args.model_b)
    results = evaluate(tasks, rec, rep)
    label = f"{args.model_a} recorded, {args.model_b} replayed, {len(tasks)} tasks"
    md = report(results, label)
    print(md)
    for name, t in results.items():
        print(f"{name}: top divergence reasons {t.reasons.most_common(3)}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.md").write_text(md, encoding="utf-8")
    (out / "results.json").write_text(
        json.dumps({k: {**vars(v), "reasons": dict(v.reasons)} for k, v in results.items()}, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
