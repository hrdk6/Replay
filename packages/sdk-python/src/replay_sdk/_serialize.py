"""Best-effort conversion of arbitrary Python objects to JSON-safe values. Never raises."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import uuid
from typing import Any

MAX_STRING = 100_000
MAX_ITEMS = 1_000
MAX_DEPTH = 32


def to_jsonable(obj: Any, depth: int = 0) -> Any:
    try:
        return _convert(obj, depth)
    except Exception:  # pragma: no cover - last resort
        return "<unserializable>"


def _convert(obj: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return "<max depth>"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_STRING else obj[:MAX_STRING] + "...<truncated>"
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, enum.Enum):
        return _convert(obj.value, depth + 1)
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= MAX_ITEMS:
                out["<truncated>"] = f"{len(obj) - MAX_ITEMS} more keys"
                break
            out[str(k)] = _convert(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = list(obj)
        out_list = [_convert(v, depth + 1) for v in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out_list.append(f"<{len(items) - MAX_ITEMS} more items>")
        return out_list
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _convert(fn(), depth + 1)
            except Exception:
                continue
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _convert(dataclasses.asdict(obj), depth + 1)
    text = repr(obj)
    return text if len(text) <= 2000 else text[:2000] + "...<truncated>"
