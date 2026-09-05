"""Step 5: Post-Deployment Validation + AI Feedback Loop — roadmap §8.4, ``seo_experiments`` (§11).

Two tables: ``seo_experiments`` (one row per merged, tracked deployment — the §8.2 prediction
being tested) and ``seo_experiment_checkpoints`` (one row per 7/14/28-day comparison). Both
baseline and actual metrics on a checkpoint are computed retroactively at measurement time,
anchored on the deployment date, so the columns here are pure storage for that comparison rather
than a live computation.

Note: the ``legacy_*`` tables preserved by migration 0001 are intentionally absent from the ORM
metadata. Autogenerate proposes dropping them on every revision; that must never be accepted.

Revision ID: 0012_seo_experiments
Revises: 0011_competitor_analysis
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import UTCDateTime

revision: str = "0012_seo_experiments"
down_revision: str | None = "0011_competitor_analysis"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "seo_experiments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("deployment_analysis_id", sa.Integer(), nullable=False),
        sa.Column("pull_request_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=True),
        sa.Column("affected_url", sa.String(2048), nullable=True),
        sa.Column("predicted_impact", sa.String(10), nullable=False),
        sa.Column("predicted_positive_confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("predicted_negative_confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("predicted_risk_level", sa.String(10), nullable=True),
        sa.Column("deployed_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(12), server_default="monitoring", nullable=False),
        sa.Column(
            "created_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["deployment_analysis_id"], ["deployment_analyses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pull_request_id"], ["github_pull_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_analysis_id", name="uq_seo_experiment_deployment_analysis"
        ),
    )
    op.create_index("ix_seo_experiments_website_id", "seo_experiments", ["website_id"])
    op.create_index("ix_seo_experiments_pull_request_id", "seo_experiments", ["pull_request_id"])
    op.create_index("ix_seo_experiments_page_id", "seo_experiments", ["page_id"])
    op.create_index(
        "ix_seo_experiments_website_status", "seo_experiments", ["website_id", "status"]
    )

    op.create_table(
        "seo_experiment_checkpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=False),
        sa.Column("checkpoint_day", sa.Integer(), nullable=False),
        sa.Column("due_at", UTCDateTime(timezone=True), nullable=False),
        sa.Column("measured_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("baseline_impressions", sa.Float(), nullable=True),
        sa.Column("baseline_clicks", sa.Float(), nullable=True),
        sa.Column("baseline_ctr", sa.Float(), nullable=True),
        sa.Column("baseline_position", sa.Float(), nullable=True),
        sa.Column("baseline_sessions", sa.Float(), nullable=True),
        sa.Column("baseline_conversions", sa.Float(), nullable=True),
        sa.Column("actual_impressions", sa.Float(), nullable=True),
        sa.Column("actual_clicks", sa.Float(), nullable=True),
        sa.Column("actual_ctr", sa.Float(), nullable=True),
        sa.Column("actual_position", sa.Float(), nullable=True),
        sa.Column("actual_sessions", sa.Float(), nullable=True),
        sa.Column("actual_conversions", sa.Float(), nullable=True),
        sa.Column("clicks_delta_pct", sa.Float(), nullable=True),
        sa.Column("impressions_delta_pct", sa.Float(), nullable=True),
        sa.Column("position_delta", sa.Float(), nullable=True),
        sa.Column("ctr_delta_pct", sa.Float(), nullable=True),
        sa.Column("sessions_delta_pct", sa.Float(), nullable=True),
        sa.Column("conversions_delta_pct", sa.Float(), nullable=True),
        sa.Column("actual_impact", sa.String(20), nullable=True),
        sa.Column("prediction_matched", sa.Boolean(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["seo_experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id", "checkpoint_day", name="uq_experiment_checkpoint_day"
        ),
    )
    op.create_index(
        "ix_seo_experiment_checkpoints_experiment_id", "seo_experiment_checkpoints",
        ["experiment_id"],
    )
    op.create_index(
        "ix_experiment_checkpoints_due", "seo_experiment_checkpoints", ["due_at", "measured_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_experiment_checkpoints_due", table_name="seo_experiment_checkpoints"
    )
    op.drop_index(
        "ix_seo_experiment_checkpoints_experiment_id", table_name="seo_experiment_checkpoints"
    )
    op.drop_table("seo_experiment_checkpoints")

    op.drop_index("ix_seo_experiments_website_status", table_name="seo_experiments")
    op.drop_index("ix_seo_experiments_page_id", table_name="seo_experiments")
    op.drop_index("ix_seo_experiments_pull_request_id", table_name="seo_experiments")
    op.drop_index("ix_seo_experiments_website_id", table_name="seo_experiments")
    op.drop_table("seo_experiments")
