"""Judge-vs-human agreement and judge bias diagnostics."""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy import stats

from replay_stats.intervals import Interval, bootstrap_statistic_ci, wilson_ci
from replay_stats.significance import TestResult

KappaWeights = Literal["linear", "quadratic"] | None


def confusion_matrix(
    rater_a: Sequence[Hashable], rater_b: Sequence[Hashable], categories: Sequence[Hashable]
) -> NDArray[np.int64]:
    """Counts with rows = rater A's label, columns = rater B's label."""
    if len(rater_a) != len(rater_b):
        raise ValueError("raters must label the same items")
    index = {c: i for i, c in enumerate(categories)}
    k = len(categories)
    mat = np.zeros((k, k), dtype=np.int64)
    for a, b in zip(rater_a, rater_b, strict=True):
        if a not in index or b not in index:
            raise ValueError(f"label not in categories: {a!r} / {b!r}")
        mat[index[a], index[b]] += 1
    return mat


def _weight_matrix(k: int, weights: KappaWeights) -> NDArray[np.float64]:
    i, j = np.indices((k, k))
    if weights is None:
        unweighted: NDArray[np.float64] = (i != j).astype(np.float64)
        return unweighted
    scale = max(k - 1, 1)
    if weights == "linear":
        return np.abs(i - j) / scale
    if weights == "quadratic":
        return ((i - j) / scale) ** 2
    raise ValueError(f"unknown weights {weights!r}")


def kappa_from_matrix(mat: NDArray[np.int64] | NDArray[np.float64], weights: KappaWeights = None) -> float:
    """Cohen's kappa (optionally weighted) from a confusion matrix.

    Returns NaN when chance disagreement is zero (e.g. both raters used one
    category for everything) - kappa is undefined there, not 1.
    """
    m = np.asarray(mat, dtype=np.float64)
    total = m.sum()
    if total == 0:
        return math.nan
    obs = m / total
    exp = np.outer(obs.sum(axis=1), obs.sum(axis=0))
    w = _weight_matrix(m.shape[0], weights)
    expected_disagreement = float(np.sum(w * exp))
    if expected_disagreement == 0.0:
        return math.nan
    return 1.0 - float(np.sum(w * obs)) / expected_disagreement


def cohen_kappa(
    rater_a: Sequence[Hashable],
    rater_b: Sequence[Hashable],
    categories: Sequence[Hashable],
    weights: KappaWeights = None,
) -> float:
    return kappa_from_matrix(confusion_matrix(rater_a, rater_b, categories), weights)


@dataclass(frozen=True)
class AgreementReport:
    n: int
    categories: list[str]
    kappa: float
    kappa_ci: Interval | None
    weighted_kappa: float | None
    raw_agreement: Interval
    confusion: list[list[int]]  # rows = judge, cols = human
    weights_used_for_weighted: str | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "categories": self.categories,
            "kappa": None if math.isnan(self.kappa) else self.kappa,
            "kappa_ci": self.kappa_ci.to_dict() if self.kappa_ci else None,
            "weighted_kappa": self.weighted_kappa,
            "weights_used_for_weighted": self.weights_used_for_weighted,
            "raw_agreement": self.raw_agreement.to_dict(),
            "confusion": self.confusion,
            "notes": self.notes,
        }


def agreement_report(
    judge: Sequence[Hashable],
    human: Sequence[Hashable],
    categories: Sequence[Hashable],
    ordinal: bool = False,
    confidence: float = 0.95,
    n_resamples: int = 2000,
    seed: int = 0,
) -> AgreementReport:
    """Cohen's kappa with a bootstrap CI, raw agreement, and confusion matrix.

    For ordinal scales the quadratic-weighted kappa is also reported, since
    unweighted kappa treats a 4-vs-5 disagreement the same as 1-vs-5.
    """
    n = len(judge)
    if n != len(human):
        raise ValueError("judge and human must label the same items")
    if n == 0:
        raise ValueError("no labelled items")
    cats = list(categories)
    index = {c: i for i, c in enumerate(cats)}
    a = np.array([index[x] for x in judge])
    b = np.array([index[x] for x in human])
    mat = confusion_matrix(judge, human, cats)
    kappa = kappa_from_matrix(mat)
    notes: list[str] = []

    def _kappa_stat(x: NDArray[np.int64], y: NDArray[np.int64]) -> float:
        m = np.zeros((len(cats), len(cats)))
        np.add.at(m, (x, y), 1)
        return kappa_from_matrix(m)

    kappa_ci: Interval | None = None
    if math.isnan(kappa):
        notes.append("kappa undefined: raters used a single category, so chance agreement is 1")
    else:
        kappa_ci = bootstrap_statistic_ci(
            [a, b], _kappa_stat, confidence=confidence, n_resamples=n_resamples, seed=seed
        )
        if kappa_ci.degenerate:
            notes.append("kappa CI unstable: many bootstrap resamples had undefined kappa")
    if n < 30:
        notes.append(f"only {n} labelled items; kappa CI will be wide")

    agree = int(np.trace(mat))
    weighted = None
    if ordinal and len(cats) > 2:
        wk = kappa_from_matrix(mat, "quadratic")
        weighted = None if math.isnan(wk) else wk
    return AgreementReport(
        n=n,
        categories=[str(c) for c in cats],
        kappa=kappa,
        kappa_ci=kappa_ci,
        weighted_kappa=weighted,
        raw_agreement=wilson_ci(agree, n, confidence),
        confusion=mat.tolist(),
        weights_used_for_weighted="quadratic" if weighted is not None else None,
        notes=notes,
    )


@dataclass(frozen=True)
class PositionBiasReport:
    """Diagnostics from judging each pair in both A/B orders."""

    n_pairs: int
    first_position_rate: Interval | None  # P(judge picks whichever answer is shown first | decisive)
    test: TestResult
    consistency_rate: Interval | None  # P(same winner in both orders)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "n_pairs": self.n_pairs,
            "first_position_rate": self.first_position_rate.to_dict() if self.first_position_rate else None,
            "test": self.test.to_dict(),
            "consistency_rate": self.consistency_rate.to_dict() if self.consistency_rate else None,
            "notes": self.notes,
        }


def position_bias(order1: Sequence[str], order2: Sequence[str], confidence: float = 0.95) -> PositionBiasReport:
    """Position bias from pairs judged in both orders.

    ``order1[i]`` / ``order2[i]`` are the judge's raw choice for pair i when
    shown as (X, Y) and then (Y, X); each is ``"A"`` (first shown), ``"B"``
    (second shown) or ``"tie"``. Because every pair appears in both orders,
    true quality differences cancel, so an unbiased judge picks position A
    50% of the time among decisive calls.
    """
    if len(order1) != len(order2):
        raise ValueError("need both orders for every pair")
    choices = [*order1, *order2]
    for c in choices:
        if c not in ("A", "B", "tie"):
            raise ValueError(f"invalid choice {c!r}")
    first = sum(1 for c in choices if c == "A")
    second = sum(1 for c in choices if c == "B")
    decisive = first + second
    notes: list[str] = []
    rate = wilson_ci(first, decisive, confidence) if decisive else None
    if decisive:
        res = stats.binomtest(first, decisive, 0.5)
        test = TestResult("binomial-first-position", float(res.pvalue), float(first), decisive)
    else:
        test = TestResult("binomial-first-position", 1.0, None, 0, "no decisive choices")
        notes.append("judge returned only ties")
    # Consistent = same underlying winner: (A then B) or (B then A) or (tie, tie).
    consistent = sum(
        1 for x, y in zip(order1, order2, strict=True) if (x, y) in {("A", "B"), ("B", "A"), ("tie", "tie")}
    )
    n = len(order1)
    consistency = wilson_ci(consistent, n, confidence) if n else None
    return PositionBiasReport(n, rate, test, consistency, notes)


@dataclass(frozen=True)
class LengthBiasReport:
    n: int
    mode: str
    # pairwise: P(judge prefers the longer answer | decisive, lengths differ by > threshold)
    prefers_longer_rate: Interval | None = None
    human_prefers_longer_rate: Interval | None = None
    # absolute: Spearman correlation between output length and score
    spearman_rho: float | None = None
    spearman_ci: Interval | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "mode": self.mode,
            "prefers_longer_rate": self.prefers_longer_rate.to_dict() if self.prefers_longer_rate else None,
            "human_prefers_longer_rate": (
                self.human_prefers_longer_rate.to_dict() if self.human_prefers_longer_rate else None
            ),
            "spearman_rho": self.spearman_rho,
            "spearman_ci": self.spearman_ci.to_dict() if self.spearman_ci else None,
            "notes": self.notes,
        }


_LENGTH_CAVEAT = (
    "Correlation with length is not proof of bias: longer answers may genuinely be better. "
    "Compare with the human rate on the same items when labels exist."
)


def length_bias_pairwise(
    len_a: Sequence[int],
    len_b: Sequence[int],
    judge_choice: Sequence[str],
    human_choice: Sequence[str | None] | None = None,
    min_relative_diff: float = 0.1,
    confidence: float = 0.95,
) -> LengthBiasReport:
    """How often the judge prefers the longer answer (ties and near-equal lengths excluded)."""

    def _rate(choices: Sequence[str | None]) -> Interval | None:
        longer = decisive = 0
        for la, lb, c in zip(len_a, len_b, choices, strict=True):
            if c not in ("A", "B"):
                continue
            hi, lo = max(la, lb), min(la, lb)
            if hi == 0 or (hi - lo) / hi < min_relative_diff:
                continue
            decisive += 1
            if (c == "A" and la > lb) or (c == "B" and lb > la):
                longer += 1
        return wilson_ci(longer, decisive, confidence) if decisive else None

    judge_rate = _rate(judge_choice)
    human_rate = _rate(human_choice) if human_choice is not None else None
    notes = [_LENGTH_CAVEAT]
    if judge_rate is None:
        notes.append("no decisive comparisons with a meaningful length difference")
    return LengthBiasReport(len(len_a), "pairwise", judge_rate, human_rate, notes=notes)


def length_bias_absolute(
    lengths: Sequence[int], scores: Sequence[float], confidence: float = 0.95, seed: int = 0
) -> LengthBiasReport:
    """Spearman correlation between answer length and absolute score."""
    n = len(lengths)
    notes = [_LENGTH_CAVEAT]
    if n < 5:
        notes.append("too few items for a correlation")
        return LengthBiasReport(n, "absolute", notes=notes)
    x = np.asarray(lengths, dtype=float)
    y = np.asarray(scores, dtype=float)
    if np.all(x == x[0]) or np.all(y == y[0]):
        notes.append("lengths or scores are constant; correlation undefined")
        return LengthBiasReport(n, "absolute", notes=notes)

    def _rho(a: NDArray[np.float64], b: NDArray[np.float64]) -> float:
        if np.all(a == a[0]) or np.all(b == b[0]):
            return math.nan
        return float(stats.spearmanr(a, b).statistic)

    rho = _rho(x, y)
    ci = bootstrap_statistic_ci([x, y], _rho, confidence=confidence, n_resamples=1000, seed=seed)
    return LengthBiasReport(n, "absolute", spearman_rho=rho, spearman_ci=ci, notes=notes)


CalibrationStatus = Literal["uncalibrated", "insufficient_data", "poor", "moderate", "good"]


def calibration_status(
    n: int, kappa: float, kappa_ci_low: float | None, min_labels: int = 30
) -> tuple[CalibrationStatus, str]:
    """Map agreement to a coarse status shown next to every verdict.

    Thresholds follow the common (and admittedly arbitrary) Landis & Koch
    bands, applied to the *lower* CI bound so a lucky small sample cannot
    earn a "good" badge.
    """
    if n == 0:
        return "uncalibrated", "no human labels yet"
    if n < min_labels:
        return "insufficient_data", f"{n} labels; need at least {min_labels}"
    if math.isnan(kappa) or kappa_ci_low is None or math.isnan(kappa_ci_low):
        # Usually every label fell in one category (e.g. all "pass"), so chance agreement is 1 and
        # agreement can't be told apart from luck. That is missing evidence, not evidence of a bad
        # judge, so it must not be reported as "poor"; it still blocks a "good" badge.
        return "insufficient_data", "kappa undefined: the labels don't vary enough to measure agreement beyond chance"
    if kappa_ci_low >= 0.6:
        return "good", f"kappa {kappa:.2f}, CI lower bound {kappa_ci_low:.2f} >= 0.60"
    if kappa_ci_low >= 0.4:
        return "moderate", f"kappa {kappa:.2f}, CI lower bound {kappa_ci_low:.2f} in [0.40, 0.60)"
    return "poor", f"kappa {kappa:.2f}, CI lower bound {kappa_ci_low:.2f} < 0.40"
