"""Experiment analysis: paired comparison of a candidate against a baseline.

Inputs are per-item run results; outputs are a plain-language verdict
(SAFE / UNSAFE / INCONCLUSIVE) plus every number behind it.

Scales
------
* Absolute judging: each completed run has a score in points, 0-100
  (pass/fail is 0 or 100; a 1-5 rubric is mapped linearly onto 0-100).
* Pairwise judging: each comparison is the candidate's preference in
  [-1, 1] (1 = candidate wins, 0 = tie, -1 = baseline wins); item scores
  are reported in points, -100..100 ("net win rate").

The non-inferiority ``margin`` is in the same points. The verdict asks:
"is candidate - baseline > -margin?"

Two analyses are always run, because replay can *diverge* (the candidate
asks for a tool call the recording cannot answer):

* completed-only: items where both arms produced an evaluable output.
* diverged-as-failure: a diverged run scores the worst possible value.

Neither is "the truth": completed-only can hide a candidate that diverges
on hard items; diverged-as-failure punishes the candidate for divergence
that may be harmless. The verdict therefore requires them to agree before
saying SAFE (see ``_combine``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from replay_stats.intervals import (
    Interval,
    bootstrap_mean_ci,
    paired_proportion_diff_ci,
    t_mean_ci,
)
from replay_stats.power import SampleSizeEstimate, noninferiority_sample_size
from replay_stats.significance import TestResult, holm, mcnemar, sign_test, wilcoxon_signed_rank

Decision = Literal["SAFE", "UNSAFE", "INCONCLUSIVE"]
RunStatus = Literal["completed", "diverged", "failed", "skipped"]
MetricKind = Literal["absolute", "pairwise"]

SCORE_MIN = 0.0
SCORE_MAX = 100.0


@dataclass(frozen=True)
class Run:
    """One execution of one arm on one dataset item."""

    status: RunStatus
    score: float | None = None  # absolute mode, completed runs only; points 0-100
    cost_usd: float | None = None
    latency_ms: float | None = None


@dataclass(frozen=True)
class Item:
    item_id: str
    baseline: Sequence[Run]
    candidate: Sequence[Run]
    # Pairwise mode: comparisons[r] is the candidate preference for repeat r, or
    # None when that repeat was not compared (an arm did not complete).
    comparisons: Sequence[float | None] = ()
    # (dimension, value) pairs, e.g. ("tag", "billing"), ("route", "/refund").
    slices: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True)
class AnalysisConfig:
    metric: MetricKind = "absolute"
    margin: float = 5.0
    confidence: float = 0.95
    min_items: int = 20
    max_divergence_rate: float = 0.2
    max_failure_rate: float = 0.1
    slice_min_items: int = 10
    power: float = 0.8
    n_resamples: int = 4000
    seed: int = 0

    def __post_init__(self) -> None:
        if self.margin < 0:
            raise ValueError("margin must be >= 0")
        if not 0.5 <= self.confidence < 1:
            raise ValueError("confidence must be in [0.5, 1)")


@dataclass
class ArmSummary:
    mean: Interval | None


@dataclass
class ComparisonBlock:
    """One analysis (completed-only or diverged-as-failure)."""

    name: str
    n_items: int
    baseline_mean: float | None
    candidate_mean: float | None
    difference: Interval | None  # interval used for the decision (conservative envelope)
    intervals: list[Interval]  # every interval computed, for transparency
    tests: list[TestResult]
    decision: Decision
    reason: str
    binary_table: dict[str, int] | None = None
    sd_of_differences: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "n_items": self.n_items,
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "difference": self.difference.to_dict() if self.difference else None,
            "intervals": [i.to_dict() for i in self.intervals],
            "tests": [t.to_dict() for t in self.tests],
            "decision": self.decision,
            "reason": self.reason,
            "binary_table": self.binary_table,
            "sd_of_differences": self.sd_of_differences,
        }


@dataclass
class SliceResult:
    dimension: str
    value: str
    n_items: int
    difference: Interval | None
    p_value: float | None
    p_adjusted: float | None
    regression: bool
    beyond_margin: bool
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "n_items": self.n_items,
            "difference": self.difference.to_dict() if self.difference else None,
            "p_value": self.p_value,
            "p_adjusted": self.p_adjusted,
            "regression": self.regression,
            "beyond_margin": self.beyond_margin,
            "note": self.note,
        }


@dataclass
class ExperimentAnalysis:
    verdict: Decision
    headline: str
    reasons: list[str]
    warnings: list[str]
    config: AnalysisConfig
    n_items: int
    completed_only: ComparisonBlock
    diverged_as_failure: ComparisonBlock
    divergence: dict[str, object]
    failures: dict[str, object]
    cost: dict[str, object]
    latency: dict[str, object]
    repeat_variance: dict[str, float | None]
    slices: list[SliceResult]
    sample_size: SampleSizeEstimate | None
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        c = self.config
        return {
            "verdict": self.verdict,
            "headline": self.headline,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "config": {
                "metric": c.metric,
                "margin": c.margin,
                "confidence": c.confidence,
                "min_items": c.min_items,
                "max_divergence_rate": c.max_divergence_rate,
                "max_failure_rate": c.max_failure_rate,
                "slice_min_items": c.slice_min_items,
                "power": c.power,
                "n_resamples": c.n_resamples,
                "seed": c.seed,
            },
            "n_items": self.n_items,
            "completed_only": self.completed_only.to_dict(),
            "diverged_as_failure": self.diverged_as_failure.to_dict(),
            "divergence": self.divergence,
            "failures": self.failures,
            "cost": self.cost,
            "latency": self.latency,
            "repeat_variance": self.repeat_variance,
            "slices": [s.to_dict() for s in self.slices],
            "sample_size": self.sample_size.to_dict() if self.sample_size else None,
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Interval helpers
# ---------------------------------------------------------------------------


def zero_variance_bound(value: float, n: int, lower_limit: float, upper_limit: float, confidence: float) -> Interval:
    """Conservative interval when every observed per-item difference equals ``value``.

    The bootstrap collapses to a point here, which would overstate certainty.
    Instead: with confidence ``1 - alpha/2`` (one-sided Clopper-Pearson for
    zero events), at most a fraction ``u = 1 - (alpha/2)^(1/n)`` of the
    population differs from ``value``, and any such item can shift the mean
    at most to the scale limits. The resulting interval is wide for small n
    and shrinks as ~3.7/n.
    """
    alpha = 1 - confidence
    u = 1 - (alpha / 2) ** (1 / n)
    low = value - u * (value - lower_limit)
    high = value + u * (upper_limit - value)
    return Interval(value, low, high, confidence, "zero-variance-bound")


def envelope(intervals: Sequence[Interval]) -> Interval:
    """Smallest interval containing all non-degenerate inputs (conservative)."""
    usable = [i for i in intervals if not i.degenerate and math.isfinite(i.low)]
    if not usable:
        raise ValueError("no usable intervals")
    first = usable[0]
    return Interval(
        first.estimate,
        min(i.low for i in usable),
        max(i.high for i in usable),
        first.confidence,
        "+".join(i.method for i in usable),
    )


def noninferiority_decision(ci: Interval, margin: float, n: int, min_items: int) -> tuple[Decision, str]:
    if n < min_items:
        return "INCONCLUSIVE", f"only {n} comparable items (minimum {min_items})"
    if ci.high < -margin:
        return (
            "UNSAFE",
            f"the whole CI ({ci.low:+.2f}, {ci.high:+.2f}) is below -{margin:g}",
        )
    if ci.low > -margin:
        return (
            "SAFE",
            f"the whole CI ({ci.low:+.2f}, {ci.high:+.2f}) is above -{margin:g}",
        )
    return (
        "INCONCLUSIVE",
        f"the CI ({ci.low:+.2f}, {ci.high:+.2f}) straddles -{margin:g}",
    )


# ---------------------------------------------------------------------------
# Per-item score extraction
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _absolute_item_scores(runs: Sequence[Run], diverged_as_failure: bool) -> list[float]:
    out: list[float] = []
    for r in runs:
        if r.status == "completed" and r.score is not None:
            out.append(float(r.score))
        elif r.status == "diverged" and diverged_as_failure:
            out.append(SCORE_MIN)
    return out


def _pairwise_item_scores(item: Item, diverged_as_failure: bool) -> list[float]:
    out: list[float] = []
    n_rep = max(len(item.baseline), len(item.candidate), len(item.comparisons))
    for r in range(n_rep):
        comp = item.comparisons[r] if r < len(item.comparisons) else None
        if comp is not None:
            out.append(100.0 * float(comp))
            continue
        if not diverged_as_failure:
            continue
        b = item.baseline[r].status if r < len(item.baseline) else "failed"
        c = item.candidate[r].status if r < len(item.candidate) else "failed"
        if b == "completed" and c == "diverged":
            out.append(-100.0)
        elif b == "diverged" and c == "completed":
            out.append(100.0)
        elif b == "diverged" and c == "diverged":
            out.append(0.0)
    return out


@dataclass
class _Paired:
    item_ids: list[str]
    baseline: NDArray[np.float64]  # per-item means (absolute) or zeros (pairwise)
    candidate: NDArray[np.float64]
    diffs: NDArray[np.float64]
    binary: bool
    table: dict[str, int] | None


def _collect(items: Sequence[Item], metric: MetricKind, diverged_as_failure: bool) -> _Paired:
    ids: list[str] = []
    base: list[float] = []
    cand: list[float] = []
    diffs: list[float] = []
    single = True
    all_binary = True
    for it in items:
        if metric == "absolute":
            b = _absolute_item_scores(it.baseline, diverged_as_failure)
            c = _absolute_item_scores(it.candidate, diverged_as_failure)
            if not b or not c:
                continue
            single = single and len(b) == 1 and len(c) == 1
            all_binary = all_binary and all(v in (SCORE_MIN, SCORE_MAX) for v in (*b, *c))
            mb, mc = float(np.mean(b)), float(np.mean(c))
            ids.append(it.item_id)
            base.append(mb)
            cand.append(mc)
            diffs.append(mc - mb)
        else:
            s = _pairwise_item_scores(it, diverged_as_failure)
            if not s:
                continue
            ids.append(it.item_id)
            base.append(0.0)
            cand.append(float(np.mean(s)))
            diffs.append(float(np.mean(s)))
    binary = metric == "absolute" and single and all_binary and len(ids) > 0
    table = None
    if binary:
        table = {"both_pass": 0, "baseline_only": 0, "candidate_only": 0, "both_fail": 0}
        for b_, c_ in zip(base, cand, strict=True):
            key = "both_pass" if b_ and c_ else "baseline_only" if b_ else "candidate_only" if c_ else "both_fail"
            table[key] += 1
    return _Paired(ids, np.array(base), np.array(cand), np.array(diffs), binary, table)


def _difference_intervals(p: _Paired, cfg: AnalysisConfig) -> list[Interval]:
    n = len(p.diffs)
    lower, upper = (-100.0, 100.0)
    if p.binary and p.table is not None:
        t = p.table
        am = paired_proportion_diff_ci(
            t["both_pass"], t["baseline_only"], t["candidate_only"], t["both_fail"], cfg.confidence
        )
        am = Interval(am.estimate * 100, am.low * 100, am.high * 100, am.confidence, am.method)
        boot = bootstrap_mean_ci(p.diffs, cfg.confidence, cfg.n_resamples, "bca", cfg.seed)
        return [am, boot]
    if n >= 2 and np.all(p.diffs == p.diffs[0]):
        return [zero_variance_bound(float(p.diffs[0]), n, lower, upper, cfg.confidence)]
    boot = bootstrap_mean_ci(p.diffs, cfg.confidence, cfg.n_resamples, "bca", cfg.seed)
    t_ci = t_mean_ci(p.diffs, cfg.confidence)
    return [boot, t_ci]


def _tests(p: _Paired, metric: MetricKind) -> list[TestResult]:
    if len(p.diffs) == 0:
        return []
    if p.binary and p.table is not None:
        return [mcnemar(p.table["baseline_only"], p.table["candidate_only"])]
    if metric == "pairwise":
        wins = int(np.count_nonzero(p.diffs > 0))
        losses = int(np.count_nonzero(p.diffs < 0))
        return [sign_test(wins, losses), wilcoxon_signed_rank(p.diffs)]
    return [wilcoxon_signed_rank(p.diffs)]


def _block(
    name: str, items: Sequence[Item], cfg: AnalysisConfig, diverged_as_failure: bool
) -> tuple[ComparisonBlock, _Paired]:
    p = _collect(items, cfg.metric, diverged_as_failure)
    n = len(p.diffs)
    if n == 0:
        return (
            ComparisonBlock(name, 0, None, None, None, [], [], "INCONCLUSIVE", "no comparable items"),
            p,
        )
    intervals = _difference_intervals(p, cfg)
    decision_ci = envelope(intervals) if len(intervals) > 1 else intervals[0]
    decision, reason = noninferiority_decision(decision_ci, cfg.margin, n, cfg.min_items)
    sd = float(np.std(p.diffs, ddof=1)) if n > 1 else None
    return (
        ComparisonBlock(
            name=name,
            n_items=n,
            baseline_mean=float(p.baseline.mean()) if cfg.metric == "absolute" else None,
            candidate_mean=float(p.candidate.mean()) if cfg.metric == "absolute" else None,
            difference=decision_ci,
            intervals=intervals,
            tests=_tests(p, cfg.metric),
            decision=decision,
            reason=reason,
            binary_table=p.table,
            sd_of_differences=sd,
        ),
        p,
    )


# ---------------------------------------------------------------------------
# Secondary metrics
# ---------------------------------------------------------------------------


def _rate_summary(items: Sequence[Item], arm: str, status: RunStatus, cfg: AnalysisConfig) -> dict[str, object]:
    fractions: list[float] = []
    runs_total = runs_hit = 0
    for it in items:
        runs = it.baseline if arm == "baseline" else it.candidate
        if not runs:
            continue
        hits = sum(1 for r in runs if r.status == status)
        runs_total += len(runs)
        runs_hit += hits
        fractions.append(hits / len(runs))
    if not fractions:
        return {"rate": None, "runs": 0, "count": 0, "interval": None}
    arr = np.array(fractions)
    # Item-level bootstrap respects clustering of repeats within an item.
    if np.all(arr == arr[0]):
        ci = zero_variance_bound(float(arr[0]), len(arr), 0.0, 1.0, cfg.confidence)
    else:
        ci = bootstrap_mean_ci(arr, cfg.confidence, cfg.n_resamples, "percentile", cfg.seed)
    return {
        "rate": runs_hit / runs_total,
        "runs": runs_total,
        "count": runs_hit,
        "interval": ci.to_dict(),
    }


def _paired_metric(items: Sequence[Item], attr: str, cfg: AnalysisConfig, completed_only: bool) -> dict[str, object]:
    base_means: list[float] = []
    cand_means: list[float] = []
    for it in items:

        def vals(runs: Sequence[Run]) -> list[float]:
            out = []
            for r in runs:
                v = getattr(r, attr)
                if v is None:
                    continue
                if completed_only and r.status != "completed":
                    continue
                out.append(float(v))
            return out

        b, c = vals(it.baseline), vals(it.candidate)
        if b and c:
            base_means.append(float(np.mean(b)))
            cand_means.append(float(np.mean(c)))
    if not base_means:
        return {"n_items": 0, "baseline": None, "candidate": None, "difference": None, "ratio": None}
    b_arr, c_arr = np.array(base_means), np.array(cand_means)

    def ci(x: NDArray[np.float64]) -> dict[str, object]:
        if len(x) >= 2 and not np.all(x == x[0]):
            return bootstrap_mean_ci(x, cfg.confidence, cfg.n_resamples, "bca", cfg.seed).to_dict()
        v = float(x.mean())
        return Interval(v, v, v, cfg.confidence, "single-value", degenerate=True).to_dict()

    ratio = float(c_arr.sum() / b_arr.sum()) if b_arr.sum() > 0 else None
    return {
        "n_items": len(b_arr),
        "baseline": ci(b_arr),
        "candidate": ci(c_arr),
        "difference": ci(c_arr - b_arr),
        "ratio": ratio,
    }


def _repeat_variance(items: Sequence[Item], arm: str) -> float | None:
    sds: list[float] = []
    for it in items:
        runs = it.baseline if arm == "baseline" else it.candidate
        scores = [float(r.score) for r in runs if r.status == "completed" and r.score is not None]
        if len(scores) >= 2:
            sds.append(float(np.std(scores, ddof=1)))
    return float(np.mean(sds)) if sds else None


def _slices(items: Sequence[Item], cfg: AnalysisConfig) -> list[SliceResult]:
    groups: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for it in items:
        for key in set(it.slices):
            groups[key].append(it)
    results: list[SliceResult] = []
    tested: list[int] = []
    pvals: list[float] = []
    for (dim, val), members in sorted(groups.items()):
        p = _collect(members, cfg.metric, diverged_as_failure=False)
        n = len(p.diffs)
        if n < cfg.slice_min_items:
            results.append(
                SliceResult(
                    dim,
                    val,
                    n,
                    None,
                    None,
                    None,
                    False,
                    False,
                    f"fewer than {cfg.slice_min_items} comparable items; not tested",
                )
            )
            continue
        intervals = _difference_intervals(p, cfg)
        ci = envelope(intervals) if len(intervals) > 1 else intervals[0]
        tests = _tests(p, cfg.metric)
        pv = tests[0].p_value if tests else 1.0
        results.append(SliceResult(dim, val, n, ci, pv, None, False, ci.high < -cfg.margin))
        tested.append(len(results) - 1)
        pvals.append(pv)
    alpha = 1 - cfg.confidence
    for idx, adj in zip(tested, holm(pvals), strict=True):
        s = results[idx]
        s.p_adjusted = adj
        assert s.difference is not None
        s.regression = adj < alpha and s.difference.estimate < 0
    return results


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def _fmt_ci(ci: Interval | None) -> str:
    if ci is None:
        return "n/a"
    pct = round(ci.confidence * 100)
    return f"{ci.estimate:+.1f} points, {pct}% CI {ci.low:+.1f} to {ci.high:+.1f}"


def _combine(
    cfg: AnalysisConfig,
    n_items: int,
    co: ComparisonBlock,
    df: ComparisonBlock,
    cand_div_rate: float | None,
    fail_rate: float | None,
) -> tuple[Decision, list[str]]:
    reasons: list[str] = []
    if n_items < cfg.min_items:
        return "INCONCLUSIVE", [f"dataset has {n_items} items; need at least {cfg.min_items}"]
    if fail_rate is not None and fail_rate > cfg.max_failure_rate:
        return "INCONCLUSIVE", [
            f"{fail_rate:.0%} of runs errored (limit {cfg.max_failure_rate:.0%}); fix provider errors and re-run"
        ]
    if co.decision == "UNSAFE":
        reasons.append(f"completed runs: {co.reason}")
        return "UNSAFE", reasons
    if co.decision == "SAFE":
        if cand_div_rate is not None and cand_div_rate > cfg.max_divergence_rate:
            return "INCONCLUSIVE", [
                f"SAFE on completed runs, but the candidate diverged from the recording in "
                f"{cand_div_rate:.0%} of runs (limit {cfg.max_divergence_rate:.0%}); replay cannot "
                "vouch for behaviour it could not evaluate"
            ]
        if df.decision != "SAFE":
            return "INCONCLUSIVE", [
                f"SAFE on completed runs, but not when diverged runs count as failures ({df.reason})"
            ]
        reasons.append(f"completed runs: {co.reason}")
        reasons.append(f"diverged-as-failure: {df.reason}")
        return "SAFE", reasons
    reasons.append(f"completed runs: {co.reason}")
    if df.decision == "UNSAFE":
        reasons.append(f"diverged-as-failure analysis is UNSAFE ({df.reason})")
    return "INCONCLUSIVE", reasons


def analyze_experiment(items: Sequence[Item], cfg: AnalysisConfig | None = None) -> ExperimentAnalysis:
    cfg = cfg or AnalysisConfig()
    for it in items:
        for r in (*it.baseline, *it.candidate):
            if r.score is not None and not SCORE_MIN <= r.score <= SCORE_MAX:
                raise ValueError(f"score {r.score} outside [0, 100] for item {it.item_id}")
        for c in it.comparisons:
            if c is not None and not -1.0 <= c <= 1.0:
                raise ValueError(f"comparison {c} outside [-1, 1] for item {it.item_id}")

    co, co_p = _block("completed_only", items, cfg, diverged_as_failure=False)
    df, _ = _block("diverged_as_failure", items, cfg, diverged_as_failure=True)

    div_b = _rate_summary(items, "baseline", "diverged", cfg)
    div_c = _rate_summary(items, "candidate", "diverged", cfg)
    fail_b = _rate_summary(items, "baseline", "failed", cfg)
    fail_c = _rate_summary(items, "candidate", "failed", cfg)
    total_runs = int(fail_b["runs"]) + int(fail_c["runs"])  # type: ignore[call-overload]
    total_failed = int(fail_b["count"]) + int(fail_c["count"])  # type: ignore[call-overload]
    fail_rate = total_failed / total_runs if total_runs else None
    cand_div_rate = div_c["rate"] if isinstance(div_c["rate"], float) else None

    verdict, reasons = _combine(cfg, len(items), co, df, cand_div_rate, fail_rate)

    warnings: list[str] = []
    slices = _slices(items, cfg)
    regressing = [s for s in slices if s.regression]
    if regressing:
        names = ", ".join(f"{s.dimension}={s.value}" for s in regressing)
        warnings.append(f"candidate regresses on slice(s) after Holm correction: {names}")
    if co.difference is not None and co.difference.method == "zero-variance-bound":
        warnings.append(
            "every item scored the same difference; the interval is a conservative bound, not a bootstrap estimate"
        )
    if co.tests and co.difference is not None:
        p = co.tests[0].p_value
        alpha = 1 - cfg.confidence
        if verdict == "SAFE" and p < alpha and co.difference.estimate < 0:
            warnings.append(
                f"the candidate is significantly worse than baseline ({co.tests[0].name} "
                f"p={p:.3g}) although within the {cfg.margin:g}-point margin"
            )

    sample_size: SampleSizeEstimate | None = None
    # "More data" only helps when the interval straddles the margin; divergence, errors or
    # too few items need a different fix, and the reasons say which.
    if (
        verdict == "INCONCLUSIVE"
        and co.decision == "INCONCLUSIVE"
        and len(co_p.diffs) >= cfg.min_items
        and co.difference is not None
        and co.sd_of_differences
    ):
        sample_size = noninferiority_sample_size(
            co.sd_of_differences, co.difference.estimate, cfg.margin, cfg.confidence, cfg.power
        )

    headline = _headline(verdict, cfg, co, sample_size, len(co_p.diffs), reasons)
    return ExperimentAnalysis(
        verdict=verdict,
        headline=headline,
        reasons=reasons,
        warnings=warnings,
        config=cfg,
        n_items=len(items),
        completed_only=co,
        diverged_as_failure=df,
        divergence={
            "baseline": div_b,
            "candidate": div_c,
            "difference": (
                (div_c["rate"] - div_b["rate"])  # type: ignore[operator]
                if div_b["rate"] is not None and div_c["rate"] is not None
                else None
            ),
        },
        failures={"baseline": fail_b, "candidate": fail_c, "overall_rate": fail_rate},
        cost=_paired_metric(items, "cost_usd", cfg, completed_only=False),
        latency=_paired_metric(items, "latency_ms", cfg, completed_only=True),
        repeat_variance={
            "baseline_mean_within_item_sd": _repeat_variance(items, "baseline"),
            "candidate_mean_within_item_sd": _repeat_variance(items, "candidate"),
        },
        slices=slices,
        sample_size=sample_size,
    )


def _headline(
    verdict: Decision,
    cfg: AnalysisConfig,
    co: ComparisonBlock,
    sample_size: SampleSizeEstimate | None,
    n: int,
    reasons: list[str],
) -> str:
    what = "net win rate" if cfg.metric == "pairwise" else "score difference"
    nums = f"{what} {_fmt_ci(co.difference)}, n={n}"
    if verdict == "SAFE":
        return f"SAFE: the candidate is not worse than baseline by more than {cfg.margin:g} points ({nums})."
    if verdict == "UNSAFE":
        return f"UNSAFE: the candidate is worse than baseline by more than the {cfg.margin:g}-point margin ({nums})."
    if co.decision != "INCONCLUSIVE" or n < cfg.min_items:
        # Blocked by divergence, errors or dataset size rather than by a wide interval.
        reason = reasons[0] if reasons else "see the analysis details"
        reason = reason.removeprefix("completed runs: ")
        return f"INCONCLUSIVE: {reason[0].upper()}{reason[1:]} ({nums})."
    tail = ""
    if sample_size is not None:
        tail = (
            f" Roughly {sample_size.n_required} items would be needed."
            if sample_size.n_required
            else f" {sample_size.explanation}"
        )
    return f"INCONCLUSIVE: not enough evidence either way ({nums}).{tail}"


def normalize_score(value: float, scale_min: float, scale_max: float) -> float:
    """Map a rubric score onto 0-100 points."""
    if scale_max <= scale_min:
        raise ValueError("scale_max must exceed scale_min")
    clipped = min(max(value, scale_min), scale_max)
    return 100.0 * (clipped - scale_min) / (scale_max - scale_min)


def describe_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


__all__ = [
    "AnalysisConfig",
    "ComparisonBlock",
    "Decision",
    "ExperimentAnalysis",
    "Item",
    "Run",
    "SliceResult",
    "analyze_experiment",
    "envelope",
    "noninferiority_decision",
    "normalize_score",
    "zero_variance_bound",
]
