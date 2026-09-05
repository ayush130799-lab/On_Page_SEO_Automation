"""The AI Feedback Loop's recalibration hook — roadmap §8.4's closing note: "this allows the AI to
learn which recommendations actually work."

What is built here is the accuracy *report*: across every measured checkpoint, how often did the
§8.2 prediction's direction match what GSC/GA4 actually showed, broken down by predicted impact
and by risk level. That is a real, usable answer to "is the prediction engine any good" the
moment enough experiments have completed.

What is deliberately **not** built is automatic re-weighting of the impact-scoring factors
(:mod:`app.services.impact.scoring`) from this accuracy data. The task brief calls that out as
optional ("doesn't need to be built now unless straightforward") for good reason: turning a
handful of completed experiments into new production weights without a human in the loop is a
calibration problem, not a straightforward code change — it needs enough sample size to be
statistically meaningful, a decision about how aggressively to move weights per cycle, and a
rollback plan for when the recalibration itself turns out wrong. Automating that on day one would
risk exactly the kind of unvalidated, self-reinforcing scoring drift the impact engine's Step 1
design was built to avoid.

:func:`suggest_weight_adjustments` is the seam that future work hangs off: it returns *proposed*
directional nudges for a human to review, never applies them. Wiring it into
``app.services.impact.engine``'s weight resolution (the same ``Setting``-row mechanism
``_resolve_weights`` already reads) is a small, well-scoped follow-up once real accuracy data
exists to justify it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import SeoExperiment, SeoExperimentCheckpoint, Website

#: Below this many measured checkpoints, an accuracy percentage is not worth reporting as a
#: number — "1/1 correct" is not evidence of anything.
MIN_SAMPLE_SIZE = 10


@dataclass
class AccuracyReport:
    website_id: int
    total_measured: int = 0
    matched: int = 0
    insufficient_data: int = 0
    by_predicted_impact: dict[str, dict[str, int]] = field(default_factory=dict)
    by_risk_level: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def accuracy_rate(self) -> float | None:
        evaluable = self.total_measured - self.insufficient_data
        if evaluable < MIN_SAMPLE_SIZE:
            return None
        return round(self.matched / evaluable, 3)

    @property
    def sample_size_sufficient(self) -> bool:
        return (self.total_measured - self.insufficient_data) >= MIN_SAMPLE_SIZE


def compute_accuracy_report(db: Session, website: Website) -> AccuracyReport:
    """How often the §8.2 prediction's direction matched reality, for this website's completed
    checkpoints. Read-only — this never changes a score or a weight."""
    report = AccuracyReport(website_id=website.id)

    rows = db.execute(
        select(SeoExperimentCheckpoint, SeoExperiment.predicted_impact, SeoExperiment.predicted_risk_level)
        .join(SeoExperiment, SeoExperimentCheckpoint.experiment_id == SeoExperiment.id)
        .where(
            SeoExperiment.website_id == website.id,
            SeoExperimentCheckpoint.measured_at.isnot(None),
        )
    ).all()

    for checkpoint, predicted_impact, predicted_risk in rows:
        report.total_measured += 1
        if checkpoint.actual_impact == "insufficient_data":
            report.insufficient_data += 1
            continue
        if checkpoint.prediction_matched:
            report.matched += 1

        bucket = report.by_predicted_impact.setdefault(predicted_impact, {"matched": 0, "total": 0})
        bucket["total"] += 1
        if checkpoint.prediction_matched:
            bucket["matched"] += 1

        risk_bucket = report.by_risk_level.setdefault(
            predicted_risk or "unknown", {"matched": 0, "total": 0}
        )
        risk_bucket["total"] += 1
        if checkpoint.prediction_matched:
            risk_bucket["matched"] += 1

    return report


@dataclass
class WeightAdjustmentSuggestion:
    factor: str
    direction: str  # "increase" | "decrease" | "hold"
    reason: str


def suggest_weight_adjustments(
    db: Session, website: Website
) -> list[WeightAdjustmentSuggestion]:
    """Proposed directional nudges to the impact-scoring weights, for a human to review.

    TODO(next): once :data:`MIN_SAMPLE_SIZE` worth of completed experiments consistently shows a
    predicted-impact category performing worse than chance, that is the signal to actually widen
    this — e.g. cross-referencing which of Step 1's five scoring factors dominated the
    recommendations behind the mismatched predictions, via each ``RecommendationScore.factors``
    JSON blob, and proposing a specific weight delta rather than a generic warning. That
    cross-reference is the well-scoped follow-up this docstring's module-level note points at.
    Deliberately not built now: it needs real accuracy data to design against, not a guess.
    """
    report = compute_accuracy_report(db, website)
    if not report.sample_size_sufficient:
        return [WeightAdjustmentSuggestion(
            factor="(none)", direction="hold",
            reason=(
                f"Only {report.total_measured - report.insufficient_data} evaluable checkpoint(s) "
                f"so far; at least {MIN_SAMPLE_SIZE} are needed before a recalibration signal "
                f"would be statistically meaningful."
            ),
        )]

    suggestions: list[WeightAdjustmentSuggestion] = []
    if report.accuracy_rate is not None and report.accuracy_rate < 0.5:
        suggestions.append(WeightAdjustmentSuggestion(
            factor="confidence",
            direction="decrease",
            reason=(
                f"Predictions matched actual outcomes only {report.accuracy_rate * 100:.0f}% of "
                f"the time across {report.total_measured} measured checkpoints — worse than "
                f"chance on a binary direction call. The confidence factor should carry less "
                f"weight until this improves; review by-category detail before changing anything."
            ),
        ))
    return suggestions
