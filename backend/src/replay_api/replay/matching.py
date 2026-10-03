"""Matching a candidate's tool calls against recorded tool results.

Rules, in order (see docs/replay.md and the Phase 0.5 spike report):

1. **Normalise** both argument sets:
   * object keys case-folded and sorted; ``null`` values dropped;
   * strings whitespace-collapsed and case-folded;
   * date strings in common formats -> ISO ``YYYY-MM-DD``;
   * plain numeric strings ("3", "2.5") -> numbers; integral floats -> ints;
   * arguments equal to the tool's JSON-schema ``default`` are dropped
     (``search(q, top_k=3)`` == ``search(q)`` when 3 is the default).
2. **Exact**: same tool name and same normalised-arguments hash. Unconsumed
   recordings are preferred, in recorded order.
3. **Fuzzy** (typed): same tool name and similarity >= ``threshold``, where
   only *free-text* leaves (strings of 2+ words) may differ; identifiers,
   dates, numbers, enums and other single-token values must be equal.
   Similarity = mean over the union of leaf paths; free-text leaves score
   max(difflib ratio, token Jaccard), mismatched typed leaves score 0.
4. Otherwise **no match** -> the run is DIVERGED at that step.

Why typed: the spike showed untyped string similarity happily matches
``2026-10-05`` to ``2026-10-06`` (ratio 0.9) or order ``A100`` to ``A101``
and silently serves the wrong recorded result. ``typed=False`` restores the
untyped v1 behaviour for comparison.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

MAX_STRING_FOR_SIMILARITY = 2000
_NUMERIC = re.compile(r"^-?(0|[1-9]\d{0,14})(\.\d+)?$")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%m/%d/%Y",
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_date(s: str) -> str | None:
    if not (6 <= len(s) <= 20) or not any(ch.isdigit() for ch in s):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_value(v: Any) -> Any:
    if isinstance(v, str):
        s = " ".join(v.split())
        date = _normalize_date(s)
        if date is not None:
            return date
        if _NUMERIC.match(s):
            f = float(s)
            return int(f) if f.is_integer() else round(f, 9)
        return s.casefold()
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        f = float(v)
        return int(f) if f.is_integer() else round(f, 9)
    if isinstance(v, dict):
        return {
            str(k).casefold(): normalize_value(x)
            for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))
            if x is not None
        }
    if isinstance(v, list | tuple):
        return [normalize_value(x) for x in v]
    return str(v)


def schema_defaults(tool_schemas: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """tool name (case-folded) -> {arg name (case-folded): normalised default}."""
    out: dict[str, dict[str, Any]] = {}
    for t in tool_schemas or []:
        props = ((t.get("parameters") or {}).get("properties") or {}) if isinstance(t, dict) else {}
        defaults = {
            str(k).casefold(): normalize_value(p["default"])
            for k, p in props.items()
            if isinstance(p, dict) and "default" in p
        }
        if defaults:
            out[str(t.get("name", "")).casefold()] = defaults
    return out


def canonical_args(name: str, args: Any, defaults: dict[str, dict[str, Any]] | None = None) -> Any:
    norm = normalize_value(args if args is not None else {})
    tool_defaults = (defaults or {}).get(name.casefold())
    if tool_defaults and isinstance(norm, dict):
        norm = {k: v for k, v in norm.items() if not (k in tool_defaults and tool_defaults[k] == v)}
    return norm


def args_hash(name: str, args: Any, defaults: dict[str, dict[str, Any]] | None = None) -> str:
    payload = json.dumps(
        [name.casefold(), canonical_args(name, args, defaults)], sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _flatten(v: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, x in v.items():
            out.update(_flatten(x, f"{prefix}.{k}" if prefix else str(k)))
        return out or {prefix or "$": {}}
    if isinstance(v, list):
        out = {}
        for i, x in enumerate(v):
            out.update(_flatten(x, f"{prefix}[{i}]"))
        return out or {prefix or "$": []}
    return {prefix or "$": v}


def _string_similarity(a: str, b: str) -> float:
    a, b = a[:MAX_STRING_FOR_SIMILARITY], b[:MAX_STRING_FOR_SIMILARITY]
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 1.0
    return max(ratio, jac)


def is_free_text(v: Any) -> bool:
    """Free text = a (normalised) string of two or more words that is not a date."""
    return isinstance(v, str) and " " in v and not _ISO_DATE.match(v)


def leaf_similarity(a: Any, b: Any, typed: bool = True) -> float:
    if a == b:
        return 1.0
    if typed:
        if is_free_text(a) and is_free_text(b):
            return _string_similarity(a, b)
        return 0.0
    if isinstance(a, str) and isinstance(b, str):
        return _string_similarity(a, b)
    if (
        isinstance(a, int | float)
        and isinstance(b, int | float)
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    ):
        denom = max(abs(a), abs(b))
        return max(0.0, 1.0 - abs(a - b) / denom) if denom else 1.0
    if isinstance(a, int | float | str) and isinstance(b, int | float | str):
        return _string_similarity(str(a), str(b))
    return 0.0


def args_similarity(
    a: Any, b: Any, typed: bool = True, name: str = "", defaults: dict[str, dict[str, Any]] | None = None
) -> float:
    fa, fb = _flatten(canonical_args(name, a, defaults)), _flatten(canonical_args(name, b, defaults))
    keys = fa.keys() | fb.keys()
    if not keys:
        return 1.0
    shared = fa.keys() & fb.keys()
    return sum(leaf_similarity(fa[k], fb[k], typed) for k in shared) / len(keys)


@dataclass
class ToolEvent:
    index: int
    kind: str  # tool | retrieval
    name: str
    arguments: Any
    result: Any
    call_id: str | None = None
    span_id: str | None = None
    source: str = "span"  # span | messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "arguments": self.arguments,
            "result": self.result,
            "call_id": self.call_id,
            "span_id": self.span_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolEvent:
        return cls(
            int(d["index"]),
            d.get("kind", "tool"),
            d["name"],
            d.get("arguments"),
            d.get("result"),
            d.get("call_id"),
            d.get("span_id"),
            d.get("source", "span"),
        )


@dataclass
class MatchResult:
    kind: Literal["exact", "fuzzy", "none"]
    event: ToolEvent | None
    score: float
    best_score: float  # best similarity seen among same-name recordings (for divergence reports)
    reused: bool = False


@dataclass
class ToolMatcher:
    events: list[ToolEvent]
    threshold: float = 0.85
    allow_reuse: bool = True
    typed: bool = True
    tool_schemas: list[dict[str, Any]] | None = None
    consumed: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._defaults = schema_defaults(self.tool_schemas)
        self._hashes = {e.index: args_hash(e.name, e.arguments, self._defaults) for e in self.events}

    def match(self, name: str, arguments: Any) -> MatchResult:
        same_name = [e for e in self.events if e.name.casefold() == name.casefold()]
        if not same_name:
            return MatchResult("none", None, 0.0, 0.0)
        h = args_hash(name, arguments, self._defaults)
        exact = [e for e in same_name if self._hashes[e.index] == h]
        fresh = [e for e in exact if e.index not in self.consumed]
        if fresh:
            self.consumed.add(fresh[0].index)
            return MatchResult("exact", fresh[0], 1.0, 1.0)
        if exact and self.allow_reuse:
            return MatchResult("exact", exact[0], 1.0, 1.0, reused=True)
        scored = sorted(
            ((args_similarity(arguments, e.arguments, self.typed, name, self._defaults), e) for e in same_name),
            key=lambda x: -x[0],
        )
        best = scored[0][0] if scored else 0.0
        for score, e in scored:
            if score < self.threshold:
                break
            if e.index not in self.consumed:
                self.consumed.add(e.index)
                return MatchResult("fuzzy", e, score, best)
        if self.allow_reuse:
            for score, e in scored:
                if score >= self.threshold:
                    return MatchResult("fuzzy", e, score, best, reused=True)
        return MatchResult("none", None, 0.0, best)
