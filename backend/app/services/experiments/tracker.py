"""Post-Deployment Validation — roadmap §8.4.

    merge -> create SeoExperiment (+ 3 scheduled checkpoints)
    daily -> measure any checkpoint whose due_at has passed
    compare -> actual metric deltas vs. the §8.2 prediction, store the verdict

Extends the existing GSC/GA4 pipeline (``app.services.metrics.aggregate_page_metrics``) rather
than building a second metrics path — the whole point of a feedback loop is that "actual
performance" means the same thing here as it does everywhere else in the platform.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    CHECKPOINT_DAYS,
    DeploymentAnalysis,
    GitHubChange,
    GitHubPullRequest,
    Page,
    SeoExperiment,
    SeoExperimentCheckpoint,
    Website,
)
from ..metrics import aggregate_page_metrics

logger = logging.getLogger(__name__)

#: A metric must move by at least this much (relative, for pct-based metrics) to count as a real
#: signal rather than noise. Position uses an absolute threshold instead (see below) because a
#: percentage change in a ranking position number is not a meaningful unit.
_SIGNAL_THRESHOLD_PCT = 0.05
#: Absolute position-change threshold, in ranks.
_SIGNAL_THRESHOLD_POSITION = 0.5
#: Below this many baseline sessions/impressions, a percentage delta is too noisy to trust —
#: a page going from 2 clicks to 6 is a real difference before dividing so is misleadingly a
#: "+200%" figure.
_MIN_BASELINE_FOR_SIGNAL = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ExperimentCreationOutcome:
    experiment_id: int | None = None
    reason: str = ""


def start_experiment_for_merged_pr(
    db: Session, website: Website, pull_request: GitHubPullRequest,
) -> ExperimentCreationOutcome:
    """Called when a tracked PR is merged: begin tracking its §8.2 prediction against reality.

    Idempotent — a PR merged-webhook redelivery (GitHub retries aggressively) must not create a
    second experiment for the same deployment analysis.
    """
    analysis = db.scalar(
        select(DeploymentAnalysis)
        .where(DeploymentAnalysis.pull_request_id == pull_request.id)
        .order_by(DeploymentAnalysis.id.desc())
        .limit(1)
    )
    if analysis is None:
        return ExperimentCreationOutcome(
            reason="No SEO impact analysis was ever run for this PR; nothing to validate."
        )

    existing = db.scalar(
        select(SeoExperiment).where(SeoExperiment.deployment_analysis_id == analysis.id)
    )
    if existing is not None:
        return ExperimentCreationOutcome(
            experiment_id=existing.id, reason="An experiment already tracks this deployment."
        )

    # The single primary affected URL: the highest-weight change that resolved to a known page.
    # A PR can touch several URLs; §8.4 tracks one deployment's outcome, not a per-URL fan-out.
    top_change = db.scalar(
        select(GitHubChange)
        .where(GitHubChange.deployment_analysis_id == analysis.id, GitHubChange.page_id.isnot(None))
        .order_by(GitHubChange.weight.desc())
        .limit(1)
    )
    if top_change is None:
        return ExperimentCreationOutcome(
            reason=(
                "None of this deployment's changes resolved to a known page, so there is no "
                "URL to measure GSC/GA4 performance against."
            )
        )

    page = db.get(Page, top_change.page_id)
    deployed_at = pull_request.merged_at or _now()

    experiment = SeoExperiment(
        website_id=website.id,
        deployment_analysis_id=analysis.id,
        pull_request_id=pull_request.id,
        page_id=page.id if page else None,
        affected_url=top_change.affected_url or (page.url if page else None),
        predicted_impact=analysis.expected_impact,
        predicted_positive_confidence=analysis.positive_confidence,
        predicted_negative_confidence=analysis.negative_confidence,
        predicted_risk_level=analysis.risk_level,
        deployed_at=deployed_at,
        status="monitoring",
    )
    db.add(experiment)
    db.flush()

    for day in CHECKPOINT_DAYS:
        db.add(SeoExperimentCheckpoint(
            experiment_id=experiment.id,
            checkpoint_day=day,
            due_at=deployed_at + timedelta(days=day),
        ))

    db.commit()
    db.refresh(experiment)

    logger.info(
        "Started SEO experiment %s for website %s: PR #%s, page %s, predicted=%s.",
        experiment.id, website.id, pull_request.number, experiment.page_id,
        experiment.predicted_impact,
    )
    return ExperimentCreationOutcome(experiment_id=experiment.id)


# ── Checkpoint measurement ──────────────────────────────────────────────────


def _pct_delta(baseline: float, actual: float) -> float | None:
    """None when a percentage isn't meaningful (zero baseline) — a page going from 0 to any
    clicks is a real change, but it has no well-defined percentage, and callers must not divide
    by zero to find out. ``_signal`` treats a zero baseline as "no trustworthy signal" via its own
    threshold check, which is the right call anyway: a jump from 0 to 3 clicks is noise, not proof
    of causation."""
    if baseline == 0:
        return None
    return (actual - baseline) / baseline


def _signal(delta_pct: float | None, *, baseline: float) -> int:
    """+1 improved, -1 worsened, 0 flat-or-untrustworthy."""
    if delta_pct is None or baseline < _MIN_BASELINE_FOR_SIGNAL:
        return 0
    if abs(delta_pct) < _SIGNAL_THRESHOLD_PCT:
        return 0
    return 1 if delta_pct > 0 else -1


def _position_signal(
    baseline: float | None, actual: float | None, *, baseline_impressions: float
) -> int:
    """Average position computed from a handful of impressions is noisy — the same volume gate
    that protects the percentage-based metrics has to apply here too, or a page with 15
    impressions and a one-rank wobble reads as a confirmed ranking improvement."""
    if baseline is None or actual is None or baseline <= 0:
        return 0
    if baseline_impressions < _MIN_BASELINE_FOR_SIGNAL:
        return 0
    delta = actual - baseline  # negative = improved (moved up the results page)
    if abs(delta) < _SIGNAL_THRESHOLD_POSITION:
        return 0
    return -1 if delta > 0 else 1


def _derive_actual_impact(
    *, has_baseline_data: bool, has_actual_data: bool, votes: list[int],
) -> str:
    if not has_baseline_data or not has_actual_data:
        return "insufficient_data"
    net = sum(votes)
    nonzero = [v for v in votes if v != 0]
    if not nonzero:
        return "neutral"
    if net >= 2:
        return "positive"
    if net <= -2:
        return "negative"
    if any(v > 0 for v in nonzero) and any(v < 0 for v in nonzero):
        return "mixed"
    return "positive" if net > 0 else "negative"


def _prediction_matched(predicted: str, actual: str) -> bool | None:
    if actual == "insufficient_data":
        return None
    if predicted == actual:
        return True
    # A "mixed" prediction is satisfied by any non-neutral actual outcome — the prediction was
    # "this could go either way", which mixed or a clear single direction both honour; only a
    # flatly neutral actual outcome contradicts it.
    if predicted == "mixed":
        return actual != "neutral"
    return False


def measure_checkpoint(db: Session, checkpoint: SeoExperimentCheckpoint) -> None:
    """Compute the baseline and actual windows for one checkpoint and store the verdict."""
    experiment = checkpoint.experiment
    if experiment.page_id is None:
        checkpoint.measured_at = _now()
        checkpoint.actual_impact = "insufficient_data"
        checkpoint.evidence = {"reason": "no page associated with this experiment"}
        return

    window = checkpoint.checkpoint_day
    # Two contiguous, non-overlapping windows of equal length: baseline is [deploy-N, deploy),
    # actual is [deploy, deploy+N). `until` is required on both — without it, aggregate_page_
    # metrics' `date >= since` has no ceiling and the baseline window would silently absorb every
    # row after deploy too as real time passes (see that function's docstring).
    baseline_end = experiment.deployed_at.date()
    actual_end = (experiment.deployed_at + timedelta(days=window)).date()

    baseline = aggregate_page_metrics(
        db, [experiment.page_id], window_days=window, today=baseline_end, until=baseline_end,
    )[experiment.page_id]
    actual = aggregate_page_metrics(
        db, [experiment.page_id], window_days=window, today=actual_end, until=actual_end,
    )[experiment.page_id]

    b_impressions = float(baseline.get("impressions") or 0)
    b_clicks = float(baseline.get("clicks") or 0)
    b_sessions = float(baseline.get("sessions") or 0)
    b_conversions = float(baseline.get("conversions") or 0)
    b_position = baseline.get("position")
    b_ctr = baseline.get("ctr")

    a_impressions = float(actual.get("impressions") or 0)
    a_clicks = float(actual.get("clicks") or 0)
    a_sessions = float(actual.get("sessions") or 0)
    a_conversions = float(actual.get("conversions") or 0)
    a_position = actual.get("position")
    a_ctr = actual.get("ctr")

    checkpoint.baseline_impressions = b_impressions
    checkpoint.baseline_clicks = b_clicks
    checkpoint.baseline_ctr = b_ctr
    checkpoint.baseline_position = b_position
    checkpoint.baseline_sessions = b_sessions
    checkpoint.baseline_conversions = b_conversions
    checkpoint.actual_impressions = a_impressions
    checkpoint.actual_clicks = a_clicks
    checkpoint.actual_ctr = a_ctr
    checkpoint.actual_position = a_position
    checkpoint.actual_sessions = a_sessions
    checkpoint.actual_conversions = a_conversions

    impressions_delta = _pct_delta(b_impressions, a_impressions)
    clicks_delta = _pct_delta(b_clicks, a_clicks)
    sessions_delta = _pct_delta(b_sessions, a_sessions)
    conversions_delta = _pct_delta(b_conversions, a_conversions)
    ctr_delta = _pct_delta(b_ctr, a_ctr) if b_ctr is not None and a_ctr is not None else None
    position_delta = (
        (a_position - b_position) if b_position is not None and a_position is not None else None
    )

    checkpoint.impressions_delta_pct = impressions_delta
    checkpoint.clicks_delta_pct = clicks_delta
    checkpoint.sessions_delta_pct = sessions_delta
    checkpoint.conversions_delta_pct = conversions_delta
    checkpoint.ctr_delta_pct = ctr_delta
    checkpoint.position_delta = position_delta

    has_baseline_data = b_impressions > 0 or b_sessions > 0
    has_actual_data = a_impressions > 0 or a_sessions > 0

    votes = [
        _signal(impressions_delta, baseline=b_impressions),
        _signal(clicks_delta, baseline=b_clicks),
        _signal(sessions_delta, baseline=b_sessions),
        _signal(conversions_delta, baseline=b_conversions),
        # CTR's meaningfulness is gated on impression volume (its denominator), not clicks.
        _signal(ctr_delta, baseline=b_impressions) if ctr_delta is not None else 0,
        _position_signal(b_position, a_position, baseline_impressions=b_impressions),
    ]

    actual_impact = _derive_actual_impact(
        has_baseline_data=has_baseline_data, has_actual_data=has_actual_data, votes=votes,
    )
    checkpoint.actual_impact = actual_impact
    checkpoint.prediction_matched = _prediction_matched(experiment.predicted_impact, actual_impact)
    checkpoint.measured_at = _now()
    checkpoint.evidence = {
        "votes": votes,
        "window_days": window,
        "baseline_window_ending": experiment.deployed_at.date().isoformat(),
        "actual_window_ending": (experiment.deployed_at + timedelta(days=window)).date().isoformat(),
    }

    logger.info(
        "Measured checkpoint day %s for experiment %s: predicted=%s actual=%s matched=%s",
        window, experiment.id, experiment.predicted_impact, actual_impact,
        checkpoint.prediction_matched,
    )




@dataclass
class DueCheckpointsOutcome:
    measured: int = 0
    experiments_completed: int = 0
    errors: list[str] = field(default_factory=list)


def run_due_checkpoints(db: Session, *, website_id: int | None = None) -> DueCheckpointsOutcome:
    """Measure every checkpoint whose ``due_at`` has passed and hasn't been measured yet.

    Runs across all websites by default (the daily scheduled entry point); ``website_id`` narrows
    it for on-demand or per-site use.
    """
    outcome = DueCheckpointsOutcome()
    now = _now()

    query = (
        select(SeoExperimentCheckpoint)
        .join(SeoExperiment, SeoExperimentCheckpoint.experiment_id == SeoExperiment.id)
        .where(
            SeoExperimentCheckpoint.measured_at.is_(None),
            SeoExperimentCheckpoint.due_at <= now,
            SeoExperiment.status == "monitoring",
        )
    )
    if website_id is not None:
        query = query.where(SeoExperiment.website_id == website_id)

    due = list(db.scalars(query))
    for checkpoint in due:
        try:
            measure_checkpoint(db, checkpoint)
            outcome.measured += 1
        except Exception as exc:
            outcome.errors.append(f"checkpoint {checkpoint.id}: {exc}")
            logger.exception("Failed to measure checkpoint %s", checkpoint.id)
    db.commit()

    # Mark experiments complete once all three checkpoints are measured.
    touched_experiment_ids = {c.experiment_id for c in due}
    for experiment_id in touched_experiment_ids:
        experiment = db.get(SeoExperiment, experiment_id)
        if experiment is None:
            continue
        if all(c.measured_at is not None for c in experiment.checkpoints):
            experiment.status = "completed"
            outcome.experiments_completed += 1
    db.commit()

    logger.info(
        "Checkpoint sweep: %d measured, %d experiment(s) completed, %d error(s).",
        outcome.measured, outcome.experiments_completed, len(outcome.errors),
    )
    return outcome
