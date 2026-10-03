"""Hypothesis tests for paired designs: McNemar, Wilcoxon signed-rank, sign test.

These are robustness checks next to the confidence intervals that drive the
verdict. They answer "is there *any* difference?" (two-sided, H0: no
difference), which is a different question from non-inferiority.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike
from scipy import stats

from replay_stats.intervals import as_1d


@dataclass(frozen=True)
class TestResult:
    name: str
    p_value: float
    statistic: float | None
    n: int
    note: str = ""

    def to_dict(self) -> dict[str, float | str | int | None]:
        return {
            "name": self.name,
            "p_value": self.p_value,
            "statistic": self.statistic,
            "n": self.n,
            "note": self.note,
        }


def mcnemar(n_base_only: int, n_cand_only: int, exact: bool | None = None) -> TestResult:
    """McNemar's test on the discordant cells of a paired 2x2 table.

    ``exact=None`` picks the exact binomial test when there are fewer than 50
    discordant pairs and the continuity-corrected chi-square otherwise.
    """
    if n_base_only < 0 or n_cand_only < 0:
        raise ValueError("counts must be non-negative")
    disc = n_base_only + n_cand_only
    if disc == 0:
        return TestResult("mcnemar", 1.0, 0.0, 0, "no discordant pairs")
    use_exact = disc < 50 if exact is None else exact
    if use_exact:
        res = stats.binomtest(min(n_base_only, n_cand_only), disc, 0.5, alternative="two-sided")
        return TestResult("mcnemar-exact", float(min(1.0, res.pvalue)), None, disc)
    chi2 = (abs(n_base_only - n_cand_only) - 1) ** 2 / disc
    p = float(stats.chi2.sf(chi2, df=1))
    return TestResult("mcnemar-chi2-cc", p, float(chi2), disc)


def wilcoxon_signed_rank(differences: ArrayLike) -> TestResult:
    """Two-sided Wilcoxon signed-rank test on paired differences.

    Uses Pratt's treatment of zero differences (zeros are ranked, then
    dropped), which is better behaved than discarding them up front when
    scores are coarse (e.g. 1-5 rubric scores with many ties).
    """
    d = as_1d(differences)
    n = d.size
    nonzero = int(np.count_nonzero(d))
    if n == 0:
        raise ValueError("empty sample")
    if nonzero == 0:
        return TestResult("wilcoxon", 1.0, 0.0, n, "all differences are zero")
    if nonzero < 2:
        return TestResult("wilcoxon", 1.0, None, n, "fewer than 2 non-zero differences")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = stats.wilcoxon(d, zero_method="pratt", alternative="two-sided", method="auto")
    p = float(res.pvalue)
    if not math.isfinite(p):
        return TestResult("wilcoxon", 1.0, None, n, "test undefined for this sample")
    return TestResult("wilcoxon", p, float(res.statistic), n)


def sign_test(wins: int, losses: int) -> TestResult:
    """Exact two-sided sign test (ties excluded). Used for pairwise judging."""
    if wins < 0 or losses < 0:
        raise ValueError("counts must be non-negative")
    n = wins + losses
    if n == 0:
        return TestResult("sign", 1.0, None, 0, "no decisive comparisons")
    res = stats.binomtest(wins, n, 0.5, alternative="two-sided")
    return TestResult("sign", float(min(1.0, res.pvalue)), float(wins), n)


def holm(p_values: list[float]) -> list[float]:
    """Holm-Bonferroni step-down adjusted p-values (controls FWER)."""
    m = len(p_values)
    if m == 0:
        return []
    for p in p_values:
        if not 0.0 <= p <= 1.0 or math.isnan(p):
            raise ValueError("p-values must be in [0, 1]")
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted
