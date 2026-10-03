"""Confidence intervals for means, proportions, and paired proportions.

Everything here is a pure function. Randomness (bootstrap) comes only from an
explicit ``seed`` so results are reproducible and testable.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import stats

BootstrapMethod = Literal["bca", "percentile"]

# Upper bound on floats materialised per bootstrap chunk (~32 MB of float64).
_MAX_CHUNK_CELLS = 4_000_000


@dataclass(frozen=True)
class Interval:
    """A point estimate with a two-sided confidence interval."""

    estimate: float
    low: float
    high: float
    confidence: float
    method: str
    # True when the sample had zero variance, so the interval collapsed to a point.
    # A collapsed interval is *not* evidence of certainty; callers should treat it
    # with suspicion (see verdict rules).
    degenerate: bool = False

    def contains(self, value: float) -> bool:
        return self.low <= value <= self.high

    def to_dict(self) -> dict[str, object]:
        return {
            "estimate": self.estimate,
            "low": self.low,
            "high": self.high,
            "confidence": self.confidence,
            "method": self.method,
            "degenerate": self.degenerate,
        }


def as_1d(x: ArrayLike) -> NDArray[np.float64]:
    arr = np.asarray(x, dtype=np.float64).reshape(-1)
    if not np.all(np.isfinite(arr)):
        raise ValueError("input contains NaN or infinite values")
    return arr


def _check_confidence(confidence: float) -> float:
    if not 0.5 <= confidence < 1.0:
        raise ValueError("confidence must be in [0.5, 1)")
    return 1.0 - confidence


def _bootstrap_means(x: NDArray[np.float64], n_resamples: int, seed: int) -> NDArray[np.float64]:
    n = x.size
    rng = np.random.default_rng(seed)
    out = np.empty(n_resamples, dtype=np.float64)
    chunk = max(1, _MAX_CHUNK_CELLS // n)
    for start in range(0, n_resamples, chunk):
        stop = min(start + chunk, n_resamples)
        idx = rng.integers(0, n, size=(stop - start, n))
        out[start:stop] = x[idx].mean(axis=1)
    return out


def bootstrap_mean_ci(
    x: ArrayLike,
    confidence: float = 0.95,
    n_resamples: int = 4000,
    method: BootstrapMethod = "bca",
    seed: int = 0,
) -> Interval:
    """Bootstrap CI for the mean of ``x``.

    For paired designs pass the per-item differences; resampling items keeps the
    pairing (and any within-item repeats) intact.

    ``bca`` is the bias-corrected and accelerated interval (Efron 1987). Its
    acceleration constant uses the closed-form jackknife of the mean.
    """
    alpha = _check_confidence(confidence)
    arr = as_1d(x)
    n = arr.size
    if n == 0:
        raise ValueError("cannot compute an interval from an empty sample")
    est = float(arr.mean())
    if n == 1 or bool(np.all(arr == arr[0])):
        return Interval(est, est, est, confidence, f"bootstrap-{method}", degenerate=True)

    boot = _bootstrap_means(arr, n_resamples, seed)
    if method == "percentile":
        lo, hi = np.quantile(boot, [alpha / 2, 1 - alpha / 2])
    elif method == "bca":
        below = np.count_nonzero(boot < est) + 0.5 * np.count_nonzero(boot == est)
        prop = float(np.clip(below / n_resamples, 1 / (n_resamples + 1), n_resamples / (n_resamples + 1)))
        z0 = float(stats.norm.ppf(prop))
        jack = (arr.sum() - arr) / (n - 1)
        d = jack.mean() - jack
        denom = 6.0 * float(np.sum(d**2)) ** 1.5
        accel = float(np.sum(d**3)) / denom if denom > 0 else 0.0
        z = stats.norm.ppf([alpha / 2, 1 - alpha / 2])
        adjusted = stats.norm.cdf(z0 + (z0 + z) / (1 - accel * (z0 + z)))
        lo, hi = np.quantile(boot, adjusted)
    else:  # pragma: no cover - guarded by Literal type
        raise ValueError(f"unknown bootstrap method {method!r}")
    return Interval(est, float(lo), float(hi), confidence, f"bootstrap-{method}")


def bootstrap_statistic_ci(
    arrays: Sequence[ArrayLike],
    statistic: Callable[..., float],
    confidence: float = 0.95,
    n_resamples: int = 2000,
    seed: int = 0,
) -> Interval:
    """Percentile bootstrap for an arbitrary statistic of row-aligned arrays.

    All arrays are resampled with the *same* indices, so paired structure is
    preserved. Resamples where the statistic is undefined (NaN) are dropped;
    if more than 10% are dropped the interval is reported as degenerate.
    """
    alpha = _check_confidence(confidence)
    cols = [np.asarray(a) for a in arrays]
    n = len(cols[0])
    if n == 0 or any(len(c) != n for c in cols):
        raise ValueError("arrays must be non-empty and the same length")
    est = float(statistic(*cols))
    rng = np.random.default_rng(seed)
    vals = np.empty(n_resamples, dtype=np.float64)
    for b in range(n_resamples):
        idx = rng.integers(0, n, size=n)
        vals[b] = statistic(*(c[idx] for c in cols))
    finite = vals[np.isfinite(vals)]
    if finite.size < 0.9 * n_resamples or not math.isfinite(est):
        return Interval(est, math.nan, math.nan, confidence, "bootstrap-percentile", degenerate=True)
    lo, hi = np.quantile(finite, [alpha / 2, 1 - alpha / 2])
    return Interval(est, float(lo), float(hi), confidence, "bootstrap-percentile")


def t_mean_ci(x: ArrayLike, confidence: float = 0.95) -> Interval:
    """Student-t interval for a mean; reported as a cross-check on the bootstrap."""
    alpha = _check_confidence(confidence)
    arr = as_1d(x)
    n = arr.size
    if n == 0:
        raise ValueError("cannot compute an interval from an empty sample")
    est = float(arr.mean())
    if n == 1:
        return Interval(est, est, est, confidence, "t", degenerate=True)
    sd = float(arr.std(ddof=1))
    if sd == 0.0:
        return Interval(est, est, est, confidence, "t", degenerate=True)
    half = float(stats.t.ppf(1 - alpha / 2, df=n - 1)) * sd / math.sqrt(n)
    return Interval(est, est - half, est + half, confidence, "t")


def wilson_ci(successes: int, n: int, confidence: float = 0.95) -> Interval:
    """Wilson score interval for a single proportion."""
    alpha = _check_confidence(confidence)
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be within [0, n]")
    z = float(stats.norm.ppf(1 - alpha / 2))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return Interval(p, low, high, confidence, "wilson")


def paired_proportion_diff_ci(
    n_both: int,
    n_base_only: int,
    n_cand_only: int,
    n_neither: int,
    confidence: float = 0.95,
) -> Interval:
    """CI for p(candidate) - p(baseline) from a paired 2x2 table.

    Uses the Agresti-Min (2005) adjusted Wald interval: add 1/2 to every cell,
    then apply the paired Wald formula. It has close-to-nominal coverage even
    with small samples or zero discordant pairs, where the plain Wald and the
    percentile bootstrap both collapse to a zero-width interval.

    Cells:
      n_both      - baseline pass, candidate pass
      n_base_only - baseline pass, candidate fail   (regressions)
      n_cand_only - baseline fail, candidate pass   (improvements)
      n_neither   - baseline fail, candidate fail
    """
    alpha = _check_confidence(confidence)
    cells = (n_both, n_base_only, n_cand_only, n_neither)
    if any(c < 0 for c in cells):
        raise ValueError("cell counts must be non-negative")
    n = sum(cells)
    if n == 0:
        raise ValueError("empty table")
    z = float(stats.norm.ppf(1 - alpha / 2))
    raw = (n_cand_only - n_base_only) / n
    b = n_base_only + 0.5
    c = n_cand_only + 0.5
    n_adj = n + 2.0
    diff = (c - b) / n_adj
    var = ((b + c) - (c - b) ** 2 / n_adj) / (n_adj**2)
    half = z * math.sqrt(max(var, 0.0))
    return Interval(raw, max(-1.0, diff - half), min(1.0, diff + half), confidence, "agresti-min")
