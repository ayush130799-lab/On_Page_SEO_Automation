"""Post-Deployment Validation and the AI Feedback Loop — roadmap §8.4.

Read-only surface over what ``app.services.experiments`` tracks: every deployment being
monitored, its checkpoints, and the accumulated prediction-accuracy report. Nothing here starts
an experiment (that happens automatically on a tracked PR's merge) or measures a checkpoint early
(that happens on the daily schedule) — an on-demand "run due checkpoints now" endpoint is
provided for operators who don't want to wait for the next scheduled sweep.
"""

from __future__ import annotations

from fastapi import APIRouter

from ...core.deps import DbSession, ReadableWebsite, WritableWebsite
from ...core.errors import NotFoundError
from ...models import SeoExperiment
from ...services.experiments import (
    compute_accuracy_report,
    run_due_checkpoints,
    suggest_weight_adjustments,
)

router = APIRouter(prefix="/api", tags=["experiments"])


def _serialise_checkpoint(c) -> dict:
    return {
        "checkpoint_day": c.checkpoint_day,
        "due_at": c.due_at.isoformat() if c.due_at else None,
        "measured_at": c.measured_at.isoformat() if c.measured_at else None,
        "baseline": {
            "impressions": c.baseline_impressions,
            "clicks": c.baseline_clicks,
            "ctr": c.baseline_ctr,
            "position": c.baseline_position,
            "sessions": c.baseline_sessions,
            "conversions": c.baseline_conversions,
        },
        "actual": {
            "impressions": c.actual_impressions,
            "clicks": c.actual_clicks,
            "ctr": c.actual_ctr,
            "position": c.actual_position,
            "sessions": c.actual_sessions,
            "conversions": c.actual_conversions,
        },
        "deltas": {
            "impressions_pct": c.impressions_delta_pct,
            "clicks_pct": c.clicks_delta_pct,
            "ctr_pct": c.ctr_delta_pct,
            "position": c.position_delta,
            "sessions_pct": c.sessions_delta_pct,
            "conversions_pct": c.conversions_delta_pct,
        },
        "actual_impact": c.actual_impact,
        "prediction_matched": c.prediction_matched,
    }


def _serialise_experiment(e: SeoExperiment, *, with_checkpoints: bool = True) -> dict:
    return {
        "id": e.id,
        "pull_request_id": e.pull_request_id,
        "page_id": e.page_id,
        "affected_url": e.affected_url,
        "predicted_impact": e.predicted_impact,
        "predicted_positive_confidence": e.predicted_positive_confidence,
        "predicted_negative_confidence": e.predicted_negative_confidence,
        "predicted_risk_level": e.predicted_risk_level,
        "deployed_at": e.deployed_at.isoformat() if e.deployed_at else None,
        "status": e.status,
        **(
            {"checkpoints": [_serialise_checkpoint(c) for c in e.checkpoints]}
            if with_checkpoints else {}
        ),
    }


@router.get("/websites/{website_id}/experiments")
def list_experiments(
    website: ReadableWebsite,
    db: DbSession,
    status: str | None = None,
    limit: int = 50,
):
    """Every tracked deployment, most recent first."""
    query = db.query(SeoExperiment).filter(SeoExperiment.website_id == website.id)
    if status:
        query = query.filter(SeoExperiment.status == status)
    experiments = query.order_by(SeoExperiment.id.desc()).limit(min(limit, 200)).all()
    return {
        "items": [_serialise_experiment(e, with_checkpoints=False) for e in experiments],
    }


@router.get("/websites/{website_id}/experiments/accuracy")
def get_accuracy_report(
    website: ReadableWebsite,
    db: DbSession,
):
    """The AI Feedback Loop's headline number: across every measured checkpoint, how often did
    the §8.2 prediction's direction match reality — overall, by predicted impact, and by risk
    level. Below the minimum sample size, ``accuracy_rate`` is null rather than a misleadingly
    precise percentage from a handful of data points.

    Registered before ``/{experiment_id}`` deliberately: FastAPI matches routes in registration
    order, and a dynamic ``int`` path segment would otherwise swallow ``.../accuracy`` as an
    attempted (and invalid) experiment id, 422-ing every call to this endpoint.
    """
    report = compute_accuracy_report(db, website)
    suggestions = suggest_weight_adjustments(db, website)
    return {
        "total_measured": report.total_measured,
        "insufficient_data": report.insufficient_data,
        "matched": report.matched,
        "accuracy_rate": report.accuracy_rate,
        "sample_size_sufficient": report.sample_size_sufficient,
        "by_predicted_impact": report.by_predicted_impact,
        "by_risk_level": report.by_risk_level,
        # Proposed directional nudges for a human to review — never applied automatically.
        # See app.services.experiments.recalibration for why.
        "weight_adjustment_suggestions": [
            {"factor": s.factor, "direction": s.direction, "reason": s.reason}
            for s in suggestions
        ],
    }


@router.post("/websites/{website_id}/experiments/run-due-checkpoints")
def trigger_due_checkpoints(
    website: WritableWebsite,
    db: DbSession,
):
    """Measure any checkpoint for this website that is already due, without waiting for the
    next scheduled sweep — useful right after lowering a checkpoint's due date in a test
    environment, or when an operator wants an up-to-date report immediately."""
    outcome = run_due_checkpoints(db, website_id=website.id)
    return {
        "measured": outcome.measured,
        "experiments_completed": outcome.experiments_completed,
        "errors": outcome.errors,
    }


@router.get("/websites/{website_id}/experiments/{experiment_id}")
def get_experiment(
    experiment_id: int,
    website: ReadableWebsite,
    db: DbSession,
):
    """Full detail for one deployment: the prediction, and each checkpoint's baseline, actual,
    deltas and verdict."""
    experiment = db.get(SeoExperiment, experiment_id)
    if experiment is None or experiment.website_id != website.id:
        raise NotFoundError(f"Experiment {experiment_id} not found on website {website.id}.")
    return _serialise_experiment(experiment)
