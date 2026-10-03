# Statistics and verdicts

All statistics live in `packages/stats` (`replay-stats`): pure functions, no I/O, with known-answer and
simulation tests.

## Scale and design

- **Paired design.** Every dataset item is run under both baseline and candidate, and comparisons are
  within items.
- **Points.** Every metric is expressed on 0–100:
  - pass/fail as 0 or 100;
  - a 1–5 rubric mapped linearly;
  - pairwise preference as a net win rate from −100 to +100.

  An item's score is the mean over its repeats.
- **Non-inferiority margin δ.** Default 5 points. The question asked is: is
  `candidate − baseline > −δ`?

## Decision rule

| Condition on the interval for `candidate − baseline` | Verdict |
|---|---|
| lower bound > −δ | SAFE |
| upper bound < −δ | UNSAFE |
| otherwise | INCONCLUSIVE |

The interval is the **envelope** (the widest combination) of every interval computed for that data:

| Outcome | Intervals | Robustness test (reported, doesn't decide) |
|---|---|---|
| Binary, one repeat | Agresti–Min adjusted paired interval; paired BCa bootstrap | McNemar (exact below 50 discordant pairs, otherwise χ² with continuity correction) |
| Continuous or ordinal | Paired BCa bootstrap; Student t | Wilcoxon signed-rank (Pratt treatment of zeros) |
| Pairwise | BCa bootstrap of per-item net preference; t | Exact sign test, Wilcoxon |
| All differences identical | One-sided Clopper–Pearson bound on the share of differing items, times the largest possible shift. The bootstrap would collapse to a point here. | — |

The verdict is overridden to INCONCLUSIVE when:
- there are fewer than `min_items` comparable items (default 20);
- more than 10% of runs errored;
- candidate divergence exceeds 20%;
- the completed-only analysis says SAFE but the diverged-as-failure analysis does not.

UNSAFE always wins when the completed-only analysis is UNSAFE.

When the verdict is INCONCLUSIVE *because the interval straddles the margin*, the report estimates the
sample size needed: `n = ((z₁₋α/₂ + z_power)·σ_d / (Δ̂ + δ))²`, using the observed σ_d and Δ̂ at 80%
power. It says so explicitly when the observed difference is already at or beyond the margin, where more
data would not help.

### Measured error rates (simulation)

| Scenario | Rate | Target |
|---|---|---|
| Pass/fail, n=300, true difference = −δ exactly: P(SAFE) | **2.6%** | ≤ 2.5% nominal (one-sided α/2) |
| Rubric 0/25/…/100, n=200, true difference = −δ: P(SAFE) | **2.8%** | ≤ 2.5% nominal |
| Rubric, n=40, true difference = −δ: P(SAFE) | **1.2%** | ≤ 2.5% (conservative at small n) |
| No true difference, n=150: P(UNSAFE) | < 1% | ≈ 0 |
| Agresti–Min coverage, n=40 | 93–98.5% (test bounds) | 95% |
| BCa coverage, n=80 | 92–98% (test bounds) | 95% |
| McNemar type I error | ≤ 6% (test bound) | 5% |
| Holm family-wise error, 10 nulls | ≤ 6% (test bound) | 5% |

The first two rates sit within Monte Carlo error (about ±0.5%) of nominal. The test suite asserts them
with explicit tolerances, so a change to the statistics code that inflates the false-SAFE rate fails CI.

## Slices

Each slice (trace tag, configured metadata keys such as `route`, and recorded model) with at least
`slice_min_items` items (default 10) is tested on its own. P-values are **Holm-adjusted** across all
slices. A slice is flagged as regressing when the adjusted p < α and the estimate is below 0. The
intervals shown per slice are **not** multiplicity-adjusted, and the UI says so. A SAFE overall verdict
with a regressing slice keeps SAFE and adds a warning; the CI policy `fail_on_slice_regression` can block
on it.

## Cost and latency

Both are averaged per item, compared pairwise, and given 95% BCa bootstrap intervals over items, with a
total ratio. Latency counts completed runs only; cost includes diverged runs, which were still billed.

## Judges and calibration

- **Agreement.**
  - Cohen's κ with a 2,000-resample percentile bootstrap CI over labelled items.
  - Raw agreement with a Wilson CI.
  - The confusion matrix.
  - For 1–5 rubrics, quadratic-weighted κ as well, since unweighted κ treats 4-vs-5 like 1-vs-5.
  - κ is reported as *undefined*, not 1, when both raters used a single category.
- **Status** comes from the lower CI bound:
  - ≥ 0.6: good
  - ≥ 0.4: moderate
  - below 0.4: poor
  - fewer than 30 labels: insufficient

  The Landis & Koch bands are conventional and somewhat arbitrary. Using the lower bound stops a lucky
  small sample from earning "good".
- **Position bias.**
  - Every pair is judged in both orders.
  - The report shows P(the first-shown answer is picked | the judge picked one), with a Wilson CI and a
    binomial test against 0.5, plus how often both orders agree on the winner.
  - Averaging the two orders cancels the bias in the verdict.
- **Length bias.**
  - Pairwise: P(the judge prefers the longer answer), considered only when the lengths differ by more
    than 10%, next to the same rate for human labels when they exist.
  - Absolute: Spearman ρ between answer length and score, with a CI.
  - Both are labelled as correlation, not proof of bias.

## Known limitations

- Bootstrap intervals undercover with very small samples or heavy skew. The `min_items` floor and the
  envelope with the t interval mitigate this but don't remove it.
- The sample-size estimate uses a normal approximation and assumes the observed effect and variance are
  the true ones. It is a planning figure, not a guarantee.
- Slices overlap (a trace has several tags), so Holm is conservative but valid. Slice intervals are
  unadjusted.
- Verdicts measure agreement with *the judge*. An uncalibrated or biased judge produces confident but
  wrong verdicts, which is why calibration status is shown loudly on every report.
- Replay holds tool results fixed. Effects that would show up only through different tool outputs, such
  as a better search query returning better documents, are invisible unless the candidate diverges, which
  is then reported.
