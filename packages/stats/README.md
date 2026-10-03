# replay-stats

The statistics engine behind Replay verdicts. Pure functions on numpy arrays; no I/O, no
database, no network. Everything that decides SAFE / UNSAFE / INCONCLUSIVE lives here so
it can be tested in isolation.

| Question | Method | Module |
|---|---|---|
| Paired pass/fail difference | Agresti–Min adjusted interval (primary) + paired BCa bootstrap; McNemar (exact below 50 discordant pairs) | `intervals`, `significance` |
| Paired score difference | Paired BCa bootstrap + Student-t cross-check; Wilcoxon signed-rank (Pratt zeros) | `intervals`, `significance` |
| Pairwise preference | Bootstrap of per-item net win rate; exact sign test | `experiment` |
| Non-inferiority | CI lower bound vs `-margin` on the conservative envelope of all intervals | `experiment` |
| Zero-variance samples | Clopper–Pearson-style bound instead of a collapsed bootstrap | `experiment.zero_variance_bound` |
| Slices | Per-slice tests, Holm-adjusted across slices | `experiment`, `significance.holm` |
| Judge vs human | Cohen's kappa + bootstrap CI, quadratic-weighted kappa for ordinal scales, raw agreement (Wilson), confusion matrix | `agreement` |
| Judge bias | Position bias from both-order judging; length preference (pairwise) / Spearman rho (absolute) | `agreement` |
| More data needed? | Normal-approximation sample size for non-inferiority | `power` |

Known limitations are documented in the docstrings and in `docs/statistics.md`.

```bash
uv run pytest packages/stats -q          # fast tests
uv run pytest packages/stats -q -m slow  # simulation tests (false-positive rate, coverage)
```
