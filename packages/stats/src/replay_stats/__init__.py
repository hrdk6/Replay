"""replay-stats: honest statistics for paired LLM experiments.

Pure functions only (numpy/scipy, no I/O). See ``experiment.analyze_experiment``
for the end-to-end verdict and the module docstrings for the assumptions
behind each method.
"""

from replay_stats.agreement import (
    AgreementReport,
    LengthBiasReport,
    PositionBiasReport,
    agreement_report,
    calibration_status,
    cohen_kappa,
    confusion_matrix,
    kappa_from_matrix,
    length_bias_absolute,
    length_bias_pairwise,
    position_bias,
)
from replay_stats.experiment import (
    AnalysisConfig,
    ExperimentAnalysis,
    Item,
    Run,
    analyze_experiment,
    noninferiority_decision,
    normalize_score,
    zero_variance_bound,
)
from replay_stats.intervals import (
    Interval,
    bootstrap_mean_ci,
    bootstrap_statistic_ci,
    paired_proportion_diff_ci,
    t_mean_ci,
    wilson_ci,
)
from replay_stats.power import SampleSizeEstimate, noninferiority_sample_size
from replay_stats.significance import TestResult, holm, mcnemar, sign_test, wilcoxon_signed_rank

__all__ = [
    "AgreementReport",
    "AnalysisConfig",
    "ExperimentAnalysis",
    "Interval",
    "Item",
    "LengthBiasReport",
    "PositionBiasReport",
    "Run",
    "SampleSizeEstimate",
    "TestResult",
    "agreement_report",
    "analyze_experiment",
    "bootstrap_mean_ci",
    "bootstrap_statistic_ci",
    "calibration_status",
    "cohen_kappa",
    "confusion_matrix",
    "holm",
    "kappa_from_matrix",
    "length_bias_absolute",
    "length_bias_pairwise",
    "mcnemar",
    "noninferiority_decision",
    "noninferiority_sample_size",
    "normalize_score",
    "paired_proportion_diff_ci",
    "position_bias",
    "sign_test",
    "t_mean_ci",
    "wilcoxon_signed_rank",
    "wilson_ci",
    "zero_variance_bound",
]
