"""Post-Deployment Validation and the AI Feedback Loop — roadmap §8.4, the ``seo_experiments``
table from §11.

    Before deployment -> Baseline metrics -> Deploy -> 7 days -> 14 days -> 28 days -> Compare

An experiment starts the moment a tracked pull request is merged (§8.4 treats "deploy" and "PR
merged to the monitored branch" as the same event, which is the correct reading for a GitHub-flow
repository) and carries the §8.2 prediction that PR's ``DeploymentAnalysis`` already made. Three
checkpoints then ask the same question at three horizons: did search performance move the way the
prediction said it would?

Both the baseline and the actual windows are computed **retroactively**, anchored on
``deployed_at`` via ``aggregate_page_metrics``'s ``today=`` parameter, rather than captured eagerly
at merge time. This means a checkpoint that runs a few hours late (a worker backlog, a missed
beat tick) still measures the *correct* calendar window — "the 7 days before deploy" and "the 7
days after deploy" — rather than whatever window happened to be current when a value was
snapshotted early.

``SeoExperiment`` is the deployment being tracked; ``SeoExperimentCheckpoint`` is one of its three
scheduled comparisons. Splitting them mirrors every other one-to-many pair already in this
codebase (``PageIntentProfile``/``KeywordOpportunity``, ``GitHubPullRequest``/
``DeploymentAnalysis``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .github_analysis import DeploymentAnalysis, GitHubPullRequest
    from .page import Page
    from .website import Website

#: The three horizons §8.4 specifies. A tuple, not a config value — changing these would silently
#: invalidate any comparison against previously-measured experiments.
CHECKPOINT_DAYS: tuple[int, ...] = (7, 14, 28)

EXPERIMENT_STATUSES = ("monitoring", "completed", "abandoned")
#: positive | negative | neutral | mixed | insufficient_data — the last is deliberately distinct
#: from "neutral": one means "we measured no meaningful change", the other means "there was
#: nothing to measure" (no GSC/GA4 connection, or the page had no traffic before or after).
ACTUAL_IMPACT_VALUES = ("positive", "negative", "neutral", "mixed", "insufficient_data")


class SeoExperiment(TimestampMixin, Base):
    """One deployment being tracked from merge through its three checkpoints."""

    __tablename__ = "seo_experiments"
    __table_args__ = (
        UniqueConstraint("deployment_analysis_id", name="uq_seo_experiment_deployment_analysis"),
        Index("ix_seo_experiments_website_status", "website_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    deployment_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("deployment_analyses.id", ondelete="CASCADE"), nullable=False
    )
    pull_request_id: Mapped[int] = mapped_column(
        ForeignKey("github_pull_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The single primary affected URL this experiment tracks. A PR can touch several URLs;
    #: §8.4's own diagram tracks one deployment's outcome, not a per-URL fan-out, so the highest-
    #: weight resolved change from the deployment analysis is used. Nullable because a PR can be
    #: merged with SEO-relevant diffs that never resolved to a known page.
    page_id: Mapped[int | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"), index=True
    )
    #: Denormalised so the tracked URL is still visible even if the page is later deleted.
    affected_url: Mapped[str | None] = mapped_column(String(2048))

    # ── The §8.2 prediction being tested, copied at merge time ──────────────
    predicted_impact: Mapped[str] = mapped_column(String(10), nullable=False)
    predicted_positive_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    predicted_negative_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    predicted_risk_level: Mapped[str | None] = mapped_column(String(10))

    deployed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: monitoring | completed | abandoned
    status: Mapped[str] = mapped_column(String(12), default="monitoring", nullable=False)

    website: Mapped["Website"] = relationship()
    page: Mapped["Page | None"] = relationship()
    pull_request: Mapped["GitHubPullRequest"] = relationship()
    deployment_analysis: Mapped["DeploymentAnalysis"] = relationship()
    checkpoints: Mapped[list["SeoExperimentCheckpoint"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan",
        order_by="SeoExperimentCheckpoint.checkpoint_day",
    )


class SeoExperimentCheckpoint(Base):
    """One scheduled comparison (day 7, 14, or 28) for an experiment.

    Baseline and actual are both windows of ``checkpoint_day`` length — "the N days before
    deploy" versus "the N days after" — rather than a single fixed baseline reused across all
    three checkpoints, so the day-28 comparison isn't measured against a day-7-shaped baseline.
    """

    __tablename__ = "seo_experiment_checkpoints"
    __table_args__ = (
        UniqueConstraint("experiment_id", "checkpoint_day", name="uq_experiment_checkpoint_day"),
        Index("ix_experiment_checkpoints_due", "due_at", "measured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("seo_experiments.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: 7 | 14 | 28 — also the window length (in days) both metric snapshots are aggregated over.
    checkpoint_day: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    measured_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # ── The N days immediately before deploy ─────────────────────────────────
    baseline_impressions: Mapped[float | None] = mapped_column(Float)
    baseline_clicks: Mapped[float | None] = mapped_column(Float)
    baseline_ctr: Mapped[float | None] = mapped_column(Float)
    baseline_position: Mapped[float | None] = mapped_column(Float)
    baseline_sessions: Mapped[float | None] = mapped_column(Float)
    baseline_conversions: Mapped[float | None] = mapped_column(Float)

    # ── The N days immediately after deploy, measured once due_at has passed ────
    actual_impressions: Mapped[float | None] = mapped_column(Float)
    actual_clicks: Mapped[float | None] = mapped_column(Float)
    actual_ctr: Mapped[float | None] = mapped_column(Float)
    actual_position: Mapped[float | None] = mapped_column(Float)
    actual_sessions: Mapped[float | None] = mapped_column(Float)
    actual_conversions: Mapped[float | None] = mapped_column(Float)

    # ── Derived comparison ───────────────────────────────────────────────────
    clicks_delta_pct: Mapped[float | None] = mapped_column(Float)
    impressions_delta_pct: Mapped[float | None] = mapped_column(Float)
    #: Absolute change; negative means the ranking improved (a lower position number is better).
    position_delta: Mapped[float | None] = mapped_column(Float)
    ctr_delta_pct: Mapped[float | None] = mapped_column(Float)
    sessions_delta_pct: Mapped[float | None] = mapped_column(Float)
    conversions_delta_pct: Mapped[float | None] = mapped_column(Float)

    #: positive | negative | neutral | mixed | insufficient_data
    actual_impact: Mapped[str | None] = mapped_column(String(20))
    #: NULL until measured. True/False only when both a prediction and a measured actual_impact
    #: with real data exist — never guessed when actual_impact is insufficient_data.
    prediction_matched: Mapped[bool | None] = mapped_column(Boolean)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    experiment: Mapped["SeoExperiment"] = relationship(back_populates="checkpoints")
