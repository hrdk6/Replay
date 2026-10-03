"""Behavioural tests for the end-to-end experiment verdict."""

from __future__ import annotations

import json

import numpy as np
import pytest
from replay_stats import AnalysisConfig, Item, Run, analyze_experiment, noninferiority_decision
from replay_stats.intervals import Interval


def _binary_items(
    base: list[int], cand: list[int], slices: list[tuple[tuple[str, str], ...]] | None = None
) -> list[Item]:
    items = []
    for i, (b, c) in enumerate(zip(base, cand, strict=True)):
        items.append(
            Item(
                item_id=f"i{i}",
                baseline=[Run("completed", 100.0 * b, cost_usd=0.01, latency_ms=100)],
                candidate=[Run("completed", 100.0 * c, cost_usd=0.02, latency_ms=150)],
                slices=slices[i] if slices else (),
            )
        )
    return items


def _rng_binary(n: int, pb: float, pc: float, rho: float, seed: int) -> tuple[list[int], list[int]]:
    rng = np.random.default_rng(seed)
    b = (rng.random(n) < pb).astype(int)
    q = (pc - rho * pb) / (1 - rho)
    indep = (rng.random(n) < q).astype(int)
    c = np.where(rng.random(n) < rho, b, indep)
    return b.tolist(), c.tolist()


CFG = AnalysisConfig(n_resamples=1000)


class TestDecisionRule:
    def test_bands(self) -> None:
        ci = lambda lo, hi: Interval(0, lo, hi, 0.95, "x")  # noqa: E731
        assert noninferiority_decision(ci(-2, 3), 5, 100, 20)[0] == "SAFE"
        assert noninferiority_decision(ci(-12, -6), 5, 100, 20)[0] == "UNSAFE"
        assert noninferiority_decision(ci(-7, 1), 5, 100, 20)[0] == "INCONCLUSIVE"
        assert noninferiority_decision(ci(-2, 3), 5, 10, 20)[0] == "INCONCLUSIVE"

    def test_zero_margin_requires_superiority(self) -> None:
        ci = Interval(0, -0.1, 3, 0.95, "x")
        assert noninferiority_decision(ci, 0.0, 100, 20)[0] == "INCONCLUSIVE"


class TestVerdicts:
    def test_clearly_better_is_safe(self) -> None:
        b, c = _rng_binary(300, 0.6, 0.75, 0.5, 1)
        res = analyze_experiment(_binary_items(b, c), CFG)
        assert res.verdict == "SAFE"
        assert res.completed_only.binary_table is not None
        assert "agresti-min" in res.completed_only.difference.method  # type: ignore[union-attr]
        assert res.headline.startswith("SAFE")

    def test_clearly_worse_is_unsafe(self) -> None:
        b, c = _rng_binary(300, 0.8, 0.55, 0.5, 2)
        res = analyze_experiment(_binary_items(b, c), CFG)
        assert res.verdict == "UNSAFE"
        assert res.completed_only.tests[0].p_value < 0.001

    def test_small_dataset_is_inconclusive_with_sample_size(self) -> None:
        b, c = _rng_binary(40, 0.7, 0.68, 0.5, 3)
        res = analyze_experiment(_binary_items(b, c), CFG)
        assert res.verdict == "INCONCLUSIVE"
        assert res.sample_size is not None
        assert "INCONCLUSIVE" in res.headline

    def test_too_few_items(self) -> None:
        res = analyze_experiment(_binary_items([1] * 5, [1] * 5), CFG)
        assert res.verdict == "INCONCLUSIVE"
        assert "need at least" in res.reasons[0]

    def test_identical_arms_large_n_safe_via_bound(self) -> None:
        res = analyze_experiment(_binary_items([1, 0] * 150, [1, 0] * 150), CFG)
        assert res.verdict == "SAFE"

    def test_identical_continuous_small_n_is_not_overconfident(self) -> None:
        items = [Item(f"i{i}", [Run("completed", 75.0)], [Run("completed", 75.0)]) for i in range(30)]
        res = analyze_experiment(items, CFG)
        assert res.completed_only.difference is not None
        assert res.completed_only.difference.method == "zero-variance-bound"
        assert res.verdict == "INCONCLUSIVE"
        assert any("conservative bound" in w for w in res.warnings)

    def test_high_divergence_blocks_safe(self) -> None:
        b, c = _rng_binary(200, 0.6, 0.8, 0.5, 4)
        items = _binary_items(b, c)
        # make 30% of candidate runs diverge
        for i in range(0, 60):
            items[i] = Item(items[i].item_id, items[i].baseline, [Run("diverged")])
        res = analyze_experiment(items, CFG)
        assert res.completed_only.decision == "SAFE"
        assert res.verdict == "INCONCLUSIVE"
        assert "diverged" in res.reasons[0]
        # The headline names the real blocker instead of "not enough evidence", and no
        # sample-size advice is given because more data would not help.
        assert res.headline.startswith("INCONCLUSIVE: SAFE on completed runs")
        assert res.sample_size is None
        cand = res.divergence["candidate"]
        assert isinstance(cand, dict)
        assert cand["rate"] == pytest.approx(0.3)

    def test_divergence_on_failures_only_flips_df_analysis(self) -> None:
        # Candidate equals baseline when it completes, but diverges on 15% of items
        # where the baseline passed; diverged-as-failure is no longer SAFE.
        base = [1] * 300
        items = _binary_items(base, base)
        for i in range(45):
            items[i] = Item(items[i].item_id, items[i].baseline, [Run("diverged")])
        res = analyze_experiment(items, AnalysisConfig(n_resamples=500, max_divergence_rate=0.5))
        assert res.completed_only.decision == "SAFE"
        assert res.diverged_as_failure.decision == "UNSAFE"
        assert res.verdict == "INCONCLUSIVE"

    def test_failure_rate_blocks_verdict(self) -> None:
        b, c = _rng_binary(100, 0.6, 0.8, 0.5, 5)
        items = _binary_items(b, c)
        for i in range(25):
            items[i] = Item(items[i].item_id, items[i].baseline, [Run("failed")])
        res = analyze_experiment(items, CFG)
        assert res.verdict == "INCONCLUSIVE"
        assert "errored" in res.reasons[0]

    def test_continuous_repeats_and_variance(self) -> None:
        rng = np.random.default_rng(6)
        items = []
        for i in range(120):
            base = [Run("completed", float(rng.choice([50, 75, 100]))) for _ in range(3)]
            cand = [Run("completed", float(rng.choice([75, 100]))) for _ in range(3)]
            items.append(Item(f"i{i}", base, cand))
        res = analyze_experiment(items, CFG)
        assert res.completed_only.binary_table is None
        assert res.verdict == "SAFE"
        assert res.repeat_variance["baseline_mean_within_item_sd"] is not None
        names = {t.name for t in res.completed_only.tests}
        assert "wilcoxon" in names

    def test_pairwise_mode(self) -> None:
        rng = np.random.default_rng(7)
        items = []
        for i in range(150):
            comp = float(rng.choice([1.0, 0.0, -1.0], p=[0.5, 0.3, 0.2]))
            items.append(Item(f"i{i}", [Run("completed")], [Run("completed")], comparisons=[comp]))
        res = analyze_experiment(items, AnalysisConfig(metric="pairwise", n_resamples=1000))
        assert res.verdict == "SAFE"
        assert res.completed_only.baseline_mean is None
        assert any(t.name == "sign" for t in res.completed_only.tests)

    def test_pairwise_divergence_counts_as_loss(self) -> None:
        items = [Item(f"i{i}", [Run("completed")], [Run("diverged")], comparisons=[None]) for i in range(30)]
        res = analyze_experiment(items, AnalysisConfig(metric="pairwise", n_resamples=200))
        assert res.completed_only.n_items == 0
        assert res.diverged_as_failure.difference is not None
        assert res.diverged_as_failure.difference.estimate == -100.0

    def test_slices_flag_only_the_regressing_slice(self) -> None:
        rng = np.random.default_rng(8)
        base, cand, slices = [], [], []
        for i in range(400):
            group = "billing" if i % 4 == 0 else "general"
            b = int(rng.random() < 0.8)
            if group == "billing":
                c = int(rng.random() < 0.35)
            else:
                c = b if rng.random() < 0.7 else int(rng.random() < 0.85)
            base.append(b)
            cand.append(c)
            slices.append((("tag", group),))
        res = analyze_experiment(_binary_items(base, cand, slices), CFG)
        by = {(s.dimension, s.value): s for s in res.slices}
        assert by[("tag", "billing")].regression
        assert not by[("tag", "general")].regression
        assert by[("tag", "billing")].p_adjusted is not None
        assert any("billing" in w for w in res.warnings)

    def test_small_slices_not_tested(self) -> None:
        b, c = _rng_binary(60, 0.7, 0.7, 0.5, 9)
        slices = [(("route", "/rare"),) if i < 3 else (("route", "/common"),) for i in range(60)]
        res = analyze_experiment(_binary_items(b, c, slices), CFG)
        rare = next(s for s in res.slices if s.value == "/rare")
        assert rare.p_value is None
        assert "not tested" in rare.note

    def test_cost_and_latency(self) -> None:
        b, c = _rng_binary(50, 0.7, 0.7, 0.5, 10)
        res = analyze_experiment(_binary_items(b, c), CFG)
        assert res.cost["ratio"] == pytest.approx(2.0)
        diff = res.latency["difference"]
        assert isinstance(diff, dict)
        assert diff["estimate"] == pytest.approx(50.0)

    def test_output_is_json_serialisable(self) -> None:
        b, c = _rng_binary(60, 0.7, 0.7, 0.5, 11)
        res = analyze_experiment(_binary_items(b, c), CFG)
        json.dumps(res.to_dict())

    def test_validates_scores(self) -> None:
        with pytest.raises(ValueError):
            analyze_experiment([Item("x", [Run("completed", 120.0)], [Run("completed", 0.0)])])
        with pytest.raises(ValueError):
            analyze_experiment(
                [Item("x", [Run("completed")], [Run("completed")], comparisons=[2.0])],
                AnalysisConfig(metric="pairwise"),
            )

    def test_significant_but_within_margin_warning(self) -> None:
        # candidate 3 points worse on average with low noise and big n
        rng = np.random.default_rng(12)
        items = []
        for i in range(800):
            base = float(rng.choice([50.0, 75.0]))
            step = float(rng.choice([-25.0, 0.0, 25.0], p=[0.16, 0.8, 0.04]))
            items.append(Item(f"i{i}", [Run("completed", base)], [Run("completed", base + step)]))
        res = analyze_experiment(items, AnalysisConfig(n_resamples=1000, margin=6.0))
        assert res.verdict == "SAFE"
        assert any("significantly worse" in w for w in res.warnings)
