"""Post-Deployment Validation and the AI Feedback Loop — roadmap §8.4.

Extends the GSC/GA4 pipeline (Step 2) rather than building a separate subsystem: a merged,
tracked PR starts a ``SeoExperiment``, three scheduled checkpoints compare actual GSC/GA4 deltas
against the §8.2 prediction that PR's deployment analysis made, and
:mod:`recalibration` turns completed checkpoints into an accuracy report.
"""

from .recalibration import (
    MIN_SAMPLE_SIZE,
    AccuracyReport,
    WeightAdjustmentSuggestion,
    compute_accuracy_report,
    suggest_weight_adjustments,
)
from .tracker import (
    DueCheckpointsOutcome,
    ExperimentCreationOutcome,
    measure_checkpoint,
    run_due_checkpoints,
    start_experiment_for_merged_pr,
)

__all__ = [
    "MIN_SAMPLE_SIZE",
    "AccuracyReport",
    "DueCheckpointsOutcome",
    "ExperimentCreationOutcome",
    "WeightAdjustmentSuggestion",
    "compute_accuracy_report",
    "measure_checkpoint",
    "run_due_checkpoints",
    "start_experiment_for_merged_pr",
    "suggest_weight_adjustments",
]
