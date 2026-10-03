"""Simulation tests: error rates and coverage of the actual verdict procedure.

These check the property that matters for a PR gate: when the candidate is
*exactly* at the non-inferiority margin (the worst case where SAFE would be
wrong), how often do we still say SAFE? With a two-sided 95% interval the
nominal one-sided rate is 2.5%; the conservative envelope should keep us at
or below that.

Tolerances are set from the Monte Carlo standard error (~0.5% at 1000 sims).
"""

from __future__ import annotations

import numpy as np
import pytest
from replay_stats import (
    AnalysisConfig,
    Item,
    Run,
    analyze_experiment,
    bootstrap_mean_ci,
    holm,
    mcnemar,
    paired_proportion_diff_ci,
)

pytestmark = pytest.mark.slow


def _paired_binary(rng: np.random.Generator, n: int, pb: float, pc: float, rho: float) -> tuple[np.ndarray, np.ndarray]:
    b = rng.random(n) < pb
    q = (pc - rho * pb) / (1 - rho)
    c = np.where(rng.random(n) < rho, b, rng.random(n) < q)
    return b.astype(int), c.astype(int)


def _items_from_binary(b: np.ndarray, c: np.ndarray) -> list[Item]:
    return [
        Item(str(i), [Run("completed", 100.0 * bi)], [Run("completed", 100.0 * ci)])
        for i, (bi, ci) in enumerate(zip(b, c, strict=True))
    ]


def _items_from_steps(rng: np.random.Generator, n: int, p_down: float, p_up: float) -> list[Item]:
    base = rng.choice([25.0, 50.0, 75.0], size=n)
    step = rng.choice([-25.0, 0.0, 25.0], size=n, p=[p_down, 1 - p_down - p_up, p_up])
    return [
        Item(str(i), [Run("completed", float(b))], [Run("completed", float(b + s))])
        for i, (b, s) in enumerate(zip(base, step, strict=True))
    ]


def test_binary_false_safe_rate_at_margin() -> None:
    """True difference = -5 points (exactly the margin): SAFE must be rare."""
    rng = np.random.default_rng(100)
    cfg = AnalysisConfig(margin=5.0, n_resamples=500)
    sims, safe = 600, 0
    for s in range(sims):
        b, c = _paired_binary(rng, 300, 0.70, 0.65, 0.6)
        res = analyze_experiment(_items_from_binary(b, c), AnalysisConfig(**{**cfg.__dict__, "seed": s}))
        safe += res.verdict == "SAFE"
    rate = safe / sims
    assert rate <= 0.025 + 0.02, rate


def test_continuous_false_safe_rate_at_margin() -> None:
    """Rubric-style scores, true difference = 25 * (0.15 - 0.35) = -5 points."""
    rng = np.random.default_rng(101)
    sims, safe = 600, 0
    for s in range(sims):
        items = _items_from_steps(rng, 200, p_down=0.35, p_up=0.15)
        res = analyze_experiment(items, AnalysisConfig(margin=5.0, n_resamples=500, seed=s))
        safe += res.verdict == "SAFE"
    rate = safe / sims
    assert rate <= 0.025 + 0.02, rate


def test_unsafe_rate_when_no_true_difference() -> None:
    """True difference = 0: saying UNSAFE (worse than margin) should essentially never happen."""
    rng = np.random.default_rng(102)
    sims, unsafe = 400, 0
    for s in range(sims):
        items = _items_from_steps(rng, 150, p_down=0.2, p_up=0.2)
        res = analyze_experiment(items, AnalysisConfig(margin=5.0, n_resamples=400, seed=s))
        unsafe += res.verdict == "UNSAFE"
    assert unsafe / sims <= 0.01


def test_power_when_candidate_is_slightly_better() -> None:
    rng = np.random.default_rng(103)
    sims, safe = 200, 0
    for s in range(sims):
        b, c = _paired_binary(rng, 400, 0.70, 0.72, 0.6)
        res = analyze_experiment(_items_from_binary(b, c), AnalysisConfig(n_resamples=300, seed=s))
        safe += res.verdict == "SAFE"
    assert safe / sims >= 0.8


def test_agresti_min_coverage_small_n() -> None:
    """Coverage of the paired-proportion interval with only 40 items."""
    rng = np.random.default_rng(104)
    pb, pc = 0.8, 0.75
    sims, covered = 4000, 0
    for _ in range(sims):
        b, c = _paired_binary(rng, 40, pb, pc, 0.5)
        ci = paired_proportion_diff_ci(
            int(np.sum(b & c)), int(np.sum(b & (1 - c))), int(np.sum((1 - b) & c)), int(np.sum((1 - b) & (1 - c)))
        )
        covered += ci.low <= pc - pb <= ci.high
    assert 0.93 <= covered / sims <= 0.985


def test_bootstrap_bca_coverage() -> None:
    rng = np.random.default_rng(105)
    sims, covered = 600, 0
    for s in range(sims):
        x = rng.choice([-25.0, 0.0, 25.0], size=80, p=[0.3, 0.45, 0.25])
        ci = bootstrap_mean_ci(x, n_resamples=999, seed=s)
        covered += ci.low <= 25 * (0.25 - 0.3) <= ci.high
    assert 0.92 <= covered / sims <= 0.98


def test_mcnemar_type_one_error() -> None:
    rng = np.random.default_rng(106)
    sims, rejected = 3000, 0
    for _ in range(sims):
        b, c = _paired_binary(rng, 120, 0.6, 0.6, 0.5)
        res = mcnemar(int(np.sum(b & (1 - c))), int(np.sum((1 - b) & c)))
        rejected += res.p_value < 0.05
    assert rejected / sims <= 0.05 + 0.01


def test_holm_controls_familywise_error() -> None:
    rng = np.random.default_rng(107)
    sims, any_reject = 4000, 0
    for _ in range(sims):
        p = rng.random(10).tolist()  # uniform p-values under the global null
        any_reject += min(holm(p)) < 0.05
    assert any_reject / sims <= 0.05 + 0.01
