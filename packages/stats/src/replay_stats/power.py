"""Sample-size estimates for paired non-inferiority comparisons.

These are planning estimates, not guarantees: they assume the true
difference and the spread of per-item differences equal what we observed,
and they use a normal approximation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy import stats


@dataclass(frozen=True)
class SampleSizeEstimate:
    n_required: int | None
    assumed_difference: float
    sd_of_differences: float
    margin: float
    power: float
    explanation: str

    def to_dict(self) -> dict[str, object]:
        return {
            "n_required": self.n_required,
            "assumed_difference": self.assumed_difference,
            "sd_of_differences": self.sd_of_differences,
            "margin": self.margin,
            "power": self.power,
            "explanation": self.explanation,
        }


def noninferiority_sample_size(
    sd_diff: float,
    assumed_diff: float,
    margin: float,
    confidence: float = 0.95,
    power: float = 0.8,
) -> SampleSizeEstimate:
    """Items needed so the CI lower bound clears ``-margin`` with the given power.

    n = ((z_{1-alpha/2} + z_{power}) * sd / (diff + margin))^2, where alpha
    comes from the two-sided ``confidence`` used for the verdict interval.
    """
    if margin < 0:
        raise ValueError("margin must be >= 0")
    if sd_diff < 0:
        raise ValueError("sd must be >= 0")
    gap = assumed_diff + margin
    if gap <= 0:
        return SampleSizeEstimate(
            None,
            assumed_diff,
            sd_diff,
            margin,
            power,
            "The observed difference is at or beyond the margin, so more data is unlikely to "
            "show the candidate is non-inferior.",
        )
    if sd_diff == 0:
        return SampleSizeEstimate(
            None,
            assumed_diff,
            sd_diff,
            margin,
            power,
            "Observed differences have zero spread; a sample-size estimate needs variation.",
        )
    alpha = 1 - confidence
    z = float(stats.norm.ppf(1 - alpha / 2) + stats.norm.ppf(power))
    n = math.ceil((z * sd_diff / gap) ** 2)
    return SampleSizeEstimate(
        max(n, 2),
        assumed_diff,
        sd_diff,
        margin,
        power,
        f"Assumes the true difference is {assumed_diff:+.2f} points and per-item differences "
        f"have SD {sd_diff:.2f} (both as observed); normal approximation, {power:.0%} power.",
    )
