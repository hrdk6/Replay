"""Known-answer tests: values checked against textbook examples or hand calculation."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
from replay_stats import (
    agreement_report,
    bootstrap_mean_ci,
    calibration_status,
    cohen_kappa,
    holm,
    kappa_from_matrix,
    mcnemar,
    noninferiority_sample_size,
    normalize_score,
    paired_proportion_diff_ci,
    position_bias,
    sign_test,
    t_mean_ci,
    wilcoxon_signed_rank,
    wilson_ci,
    zero_variance_bound,
)
from replay_stats.agreement import length_bias_absolute, length_bias_pairwise


class TestMcNemar:
    def test_chi2_with_continuity_matches_agresti_example(self) -> None:
        # Agresti, Categorical Data Analysis: presidential approval, discordant 150 vs 86.
        res = mcnemar(150, 86, exact=False)
        assert res.statistic == pytest.approx(63**2 / 236, rel=1e-12)
        assert res.p_value == pytest.approx(4.115e-05, rel=1e-2)

    def test_exact_binomial(self) -> None:
        # 1 vs 9 discordant: p = 2 * P(X <= 1 | n=10, 0.5) = 2 * 11/1024
        res = mcnemar(1, 9)
        assert res.name == "mcnemar-exact"
        assert res.p_value == pytest.approx(22 / 1024, rel=1e-12)

    def test_no_discordant_pairs(self) -> None:
        assert mcnemar(0, 0).p_value == 1.0

    def test_symmetric(self) -> None:
        assert mcnemar(3, 12).p_value == pytest.approx(mcnemar(12, 3).p_value)

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            mcnemar(-1, 2)


class TestAgrestiMin:
    def test_hand_calculation(self) -> None:
        # n=100, 10 regressions, 20 improvements.
        ci = paired_proportion_diff_ci(60, 10, 20, 10)
        b, c, n = 10.5, 20.5, 102.0
        z = 1.959963984540054
        centre = (c - b) / n
        half = z * math.sqrt(((b + c) - (c - b) ** 2 / n) / n**2)
        assert ci.estimate == pytest.approx(0.10)
        assert ci.low == pytest.approx(centre - half, abs=1e-12)
        assert ci.high == pytest.approx(centre + half, abs=1e-12)
        assert ci.low == pytest.approx(-0.007243, abs=1e-5)
        assert ci.high == pytest.approx(0.203321, abs=1e-5)

    def test_zero_discordant_has_nonzero_width(self) -> None:
        ci = paired_proportion_diff_ci(30, 0, 0, 20)
        assert ci.estimate == 0.0
        assert ci.low < 0 < ci.high
        assert not ci.degenerate

    def test_clipped_to_unit_range(self) -> None:
        ci = paired_proportion_diff_ci(0, 0, 5, 0)
        assert -1.0 <= ci.low <= ci.high <= 1.0


class TestWilson:
    def test_newcombe_example(self) -> None:
        # Newcombe (1998) Table I: 81/263 -> 0.2553 to 0.3662
        ci = wilson_ci(81, 263)
        assert ci.low == pytest.approx(0.2553, abs=5e-5)
        assert ci.high == pytest.approx(0.3662, abs=5e-5)

    def test_extremes(self) -> None:
        assert wilson_ci(0, 10).low == 0.0
        assert wilson_ci(10, 10).high == 1.0


class TestBootstrapAndT:
    def test_reproducible_with_seed(self) -> None:
        x = np.random.default_rng(1).normal(size=50)
        a = bootstrap_mean_ci(x, seed=7)
        b = bootstrap_mean_ci(x, seed=7)
        assert (a.low, a.high) == (b.low, b.high)

    def test_bca_close_to_t_for_normal_data(self) -> None:
        x = np.random.default_rng(2).normal(loc=3.0, scale=2.0, size=400)
        boot = bootstrap_mean_ci(x, n_resamples=8000, seed=3)
        t = t_mean_ci(x)
        assert boot.low == pytest.approx(t.low, abs=0.06)
        assert boot.high == pytest.approx(t.high, abs=0.06)

    def test_percentile_method(self) -> None:
        x = np.arange(20, dtype=float)
        ci = bootstrap_mean_ci(x, method="percentile", seed=0)
        assert ci.low < 9.5 < ci.high

    def test_degenerate_sample(self) -> None:
        ci = bootstrap_mean_ci([2.0, 2.0, 2.0])
        assert ci.degenerate
        assert ci.low == ci.high == 2.0

    def test_t_interval_formula(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        ci = t_mean_ci(x)
        # mean 3, sd sqrt(2.5), t_{0.975,4} = 2.7764451
        half = 2.7764451051977987 * math.sqrt(2.5) / math.sqrt(5)
        assert ci.low == pytest.approx(3 - half)
        assert ci.high == pytest.approx(3 + half)

    def test_rejects_nan(self) -> None:
        with pytest.raises(ValueError):
            bootstrap_mean_ci([1.0, float("nan")])

    def test_rejects_bad_confidence(self) -> None:
        with pytest.raises(ValueError):
            t_mean_ci([1.0, 2.0], confidence=1.0)


class TestZeroVarianceBound:
    def test_hand_calculation(self) -> None:
        ci = zero_variance_bound(0.0, 100, -100.0, 100.0, 0.95)
        u = 1 - 0.025 ** (1 / 100)
        assert u == pytest.approx(0.036217, abs=1e-6)
        assert ci.low == pytest.approx(-100 * u)
        assert ci.high == pytest.approx(100 * u)

    def test_shrinks_with_n(self) -> None:
        small = zero_variance_bound(0.0, 20, -100, 100, 0.95)
        large = zero_variance_bound(0.0, 2000, -100, 100, 0.95)
        assert (large.high - large.low) < (small.high - small.low) / 50


class TestWilcoxonAndSign:
    def test_all_zero(self) -> None:
        res = wilcoxon_signed_rank([0, 0, 0, 0])
        assert res.p_value == 1.0

    def test_clear_shift(self) -> None:
        d = np.random.default_rng(0).normal(1.0, 0.5, size=40)
        assert wilcoxon_signed_rank(d).p_value < 1e-5

    def test_symmetric_noise(self) -> None:
        d = np.array([-2, -1, 1, 2, -3, 3, -1, 1, 0, 0], dtype=float)
        assert wilcoxon_signed_rank(d).p_value > 0.9

    def test_sign_test_exact(self) -> None:
        # 9 wins, 1 loss: same distribution as McNemar 1 vs 9
        assert sign_test(9, 1).p_value == pytest.approx(22 / 1024)
        assert sign_test(0, 0).p_value == 1.0


class TestHolm:
    def test_known_answer(self) -> None:
        assert holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])

    def test_monotone_and_capped(self) -> None:
        adj = holm([0.5, 0.6, 0.9])
        assert adj == pytest.approx([1.0, 1.0, 1.0])

    def test_empty(self) -> None:
        assert holm([]) == []

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            holm([1.5])


class TestKappa:
    def test_unweighted_known_answer(self) -> None:
        assert kappa_from_matrix(np.array([[20, 5], [10, 15]])) == pytest.approx(0.4)

    def test_quadratic_weighted_hand_calculation(self) -> None:
        assert cohen_kappa([0, 1, 2], [0, 2, 2], [0, 1, 2], weights="quadratic") == pytest.approx(0.8)

    def test_perfect_agreement(self) -> None:
        assert cohen_kappa(["a", "b", "a"], ["a", "b", "a"], ["a", "b"]) == pytest.approx(1.0)

    def test_undefined_when_single_category(self) -> None:
        assert math.isnan(cohen_kappa(["a", "a"], ["a", "a"], ["a", "b"]))

    def test_report(self) -> None:
        rng = np.random.default_rng(0)
        human = rng.integers(0, 2, size=200)
        judge = np.where(rng.random(200) < 0.85, human, 1 - human)
        rep = agreement_report(list(judge), list(human), [0, 1], seed=1)
        assert rep.n == 200
        assert rep.kappa_ci is not None
        assert rep.kappa_ci.low < rep.kappa < rep.kappa_ci.high
        assert 0.55 < rep.kappa < 0.85
        assert sum(sum(r) for r in rep.confusion) == 200
        json.dumps(rep.to_dict())

    def test_report_ordinal_weighted(self) -> None:
        rep = agreement_report([1, 2, 3, 4, 5] * 8, [1, 2, 3, 5, 5] * 8, [1, 2, 3, 4, 5], ordinal=True)
        assert rep.weighted_kappa is not None
        assert rep.weighted_kappa > rep.kappa


class TestCalibrationStatus:
    @pytest.mark.parametrize(
        ("n", "kappa", "low", "expected"),
        [
            (0, math.nan, None, "uncalibrated"),
            (10, 0.9, 0.7, "insufficient_data"),
            (50, 0.8, 0.65, "good"),
            (50, 0.7, 0.5, "moderate"),
            (50, 0.5, 0.2, "poor"),
            # Undefined kappa (one category) is missing evidence, not disagreement.
            (50, math.nan, None, "insufficient_data"),
        ],
    )
    def test_bands(self, n: int, kappa: float, low: float | None, expected: str) -> None:
        assert calibration_status(n, kappa, low)[0] == expected


class TestBias:
    def test_position_bias_detects_first_position_preference(self) -> None:
        # Judge always picks whatever is shown first.
        rep = position_bias(["A"] * 40, ["A"] * 40)
        assert rep.first_position_rate is not None
        assert rep.first_position_rate.estimate == 1.0
        assert rep.test.p_value < 1e-10
        assert rep.consistency_rate is not None
        assert rep.consistency_rate.estimate == 0.0

    def test_position_bias_unbiased_consistent_judge(self) -> None:
        rep = position_bias(["A", "B"] * 20, ["B", "A"] * 20)
        assert rep.first_position_rate is not None
        assert rep.first_position_rate.estimate == 0.5
        assert rep.consistency_rate is not None
        assert rep.consistency_rate.estimate == 1.0

    def test_length_bias_pairwise(self) -> None:
        rep = length_bias_pairwise([100, 10, 50], [10, 100, 50], ["A", "B", "A"])
        # third pair has equal length so it's excluded; judge chose longer in both others
        assert rep.prefers_longer_rate is not None
        assert rep.prefers_longer_rate.estimate == 1.0

    def test_length_bias_absolute(self) -> None:
        lengths = list(range(1, 41))
        rep = length_bias_absolute(lengths, [float(x) for x in lengths])
        assert rep.spearman_rho == pytest.approx(1.0)

    def test_invalid_choice(self) -> None:
        with pytest.raises(ValueError):
            position_bias(["X"], ["A"])


class TestPower:
    def test_hand_calculation(self) -> None:
        est = noninferiority_sample_size(sd_diff=20, assumed_diff=0, margin=5)
        z = 1.959963984540054 + 0.8416212335729143
        assert est.n_required == math.ceil((z * 20 / 5) ** 2) == 126

    def test_beyond_margin(self) -> None:
        assert noninferiority_sample_size(10, -6, 5).n_required is None

    def test_zero_sd(self) -> None:
        assert noninferiority_sample_size(0, 0, 5).n_required is None


def test_normalize_score() -> None:
    assert normalize_score(1, 1, 5) == 0.0
    assert normalize_score(5, 1, 5) == 100.0
    assert normalize_score(3, 1, 5) == 50.0
    assert normalize_score(9, 1, 5) == 100.0
    with pytest.raises(ValueError):
        normalize_score(1, 5, 5)
