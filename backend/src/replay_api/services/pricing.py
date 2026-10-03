"""Token prices used for cost reporting and budget enforcement.

Prices change; this table is a dated snapshot. Unknown models fall back to a
deliberately high default so budgets err on the side of stopping early, and
results are flagged ``estimated``. Candidates and judges can override prices
with ``pricing: {input_per_mtok, output_per_mtok}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

PRICES_AS_OF = "2026-09-25"


@dataclass(frozen=True)
class Price:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    known: bool = True


def _p(i: str, o: str) -> Price:
    return Price(Decimal(i), Decimal(o))


# USD per million tokens. Matched by exact id first, then longest prefix.
PRICES: dict[str, Price] = {
    # Anthropic (first-party API list prices)
    "claude-fable-5-1": _p("10", "50"),
    "claude-fable-5": _p("10", "50"),
    "claude-opus-5-5": _p("4", "20"),
    "claude-opus-5": _p("5", "25"),
    "claude-opus-4-8": _p("5", "25"),
    "claude-opus-4-7": _p("5", "25"),
    "claude-opus-4-6": _p("5", "25"),
    "claude-sonnet-5-5": _p("2", "10"),
    "claude-sonnet-5": _p("2", "10"),
    "claude-sonnet-4-6": _p("3", "15"),
    "claude-haiku-4-5": _p("1", "5"),
    # OpenAI (verify against current pricing before relying on these)
    "gpt-5": _p("1.25", "10"),
    "gpt-5-mini": _p("0.25", "2"),
    "gpt-5-nano": _p("0.05", "0.40"),
    "gpt-4.1": _p("2", "8"),
    "gpt-4.1-mini": _p("0.40", "1.60"),
    "gpt-4.1-nano": _p("0.10", "0.40"),
    "gpt-4o": _p("2.50", "10"),
    "gpt-4o-mini": _p("0.15", "0.60"),
    "o4-mini": _p("1.10", "4.40"),
    # Built-in simulator: free.
    "sim-": _p("0", "0"),
}

UNKNOWN_PRICE = Price(Decimal("15"), Decimal("75"), known=False)


def price_for(model: str | None, override: dict[str, Any] | None = None) -> Price:
    if override and "input_per_mtok" in override and "output_per_mtok" in override:
        return Price(Decimal(str(override["input_per_mtok"])), Decimal(str(override["output_per_mtok"])))
    if not model:
        return UNKNOWN_PRICE
    m = model.lower()
    if m in PRICES:
        return PRICES[m]
    best = max((k for k in PRICES if m.startswith(k)), key=len, default=None)
    return PRICES[best] if best else UNKNOWN_PRICE


def cost_usd(price: Price, input_tokens: int | None, output_tokens: int | None) -> Decimal:
    i = Decimal(input_tokens or 0)
    o = Decimal(output_tokens or 0)
    return (i * price.input_per_mtok + o * price.output_per_mtok) / Decimal(1_000_000)


def estimate_tokens_from_text(chars: int) -> int:
    """Rough token estimate (~4 chars/token) for budgeting when usage is unknown."""
    return max(1, chars // 4 + 1)
