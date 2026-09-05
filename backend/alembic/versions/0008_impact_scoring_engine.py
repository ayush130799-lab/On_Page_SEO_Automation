"""Step 1: per-recommendation impact scores, and the second intent axis.

``recommendation_scores`` is the grain roadmap §4.4 requires and the existing schema could not
express: one row per page *per recommendation type*, so "rewrite title" and "add alt text" on the
same page can carry different search/user-activity/business scores, priorities and confidences.
``ai_recommendations`` stays as the per-page LLM audit trail.

``page_intent_profiles`` gains §6.1's page-type axis (commercial | informational | hybrid), the
content hash that lets a changed page be re-classified rather than keeping its first answer for
ever, and a record of which evidence produced a mismatch verdict.

Note: the ``legacy_*`` tables preserved by migration 0001 are intentionally absent from the ORM
metadata. Autogenerate proposes dropping them on every revision; that must never be accepted.

Revision ID: 0008_impact_scoring_engine
Revises: 0007_search_intent_keywords
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import UTCDateTime

revision: str = "0008_impact_scoring_engine"
down_revision: str | None = "0007_search_intent_keywords"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_scores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("crawl_run_id", sa.Integer(), nullable=True),
        sa.Column("ai_recommendation_id", sa.Integer(), nullable=True),
        # what is being recommended
        sa.Column("recommendation_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("current_state", sa.Text(), nullable=True),
        sa.Column("recommended_state", sa.Text(), nullable=True),
        # keyword / intent context
        sa.Column("primary_keyword", sa.String(255), nullable=True),
        sa.Column("secondary_keywords", sa.JSON(), nullable=True),
        sa.Column("search_intent", sa.String(32), nullable=True),
        # §4.4 — two objectives kept separate, plus the blend
        sa.Column("search_impact_score", sa.Float(), nullable=True),
        sa.Column("user_activity_score", sa.Float(), nullable=True),
        sa.Column("business_impact_score", sa.Float(), nullable=True),
        sa.Column("overall_priority", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        # §7.2 banding
        sa.Column("priority_level", sa.String(4), nullable=True),
        sa.Column("severity", sa.String(20), nullable=True),
        sa.Column("effort", sa.String(10), nullable=True),
        # §9.1 — never a bare number
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("expected_outcome", sa.Text(), nullable=True),
        # provenance
        sa.Column("tier", sa.String(20), nullable=True),
        sa.Column("factors", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        # lifecycle
        sa.Column("status", sa.String(20), server_default="detected", nullable=False),
        sa.Column("scored_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["crawl_run_id"], ["crawl_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["ai_recommendation_id"], ["ai_recommendations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_id", "recommendation_type", name="uq_rec_score_page_type"),
    )
    op.create_index("ix_recommendation_scores_website_id", "recommendation_scores", ["website_id"])
    op.create_index("ix_recommendation_scores_page_id", "recommendation_scores", ["page_id"])
    op.create_index(
        "ix_rec_score_website_priority", "recommendation_scores",
        ["website_id", "overall_priority"],
    )
    op.create_index(
        "ix_rec_score_website_level", "recommendation_scores", ["website_id", "priority_level"]
    )
    op.create_index("ix_rec_score_status", "recommendation_scores", ["website_id", "status"])

    # ── §6.1 second axis and re-classification support ──────────────────────
    op.add_column("page_intent_profiles", sa.Column("page_type", sa.String(16), nullable=True))
    op.add_column("page_intent_profiles", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column(
        "page_intent_profiles", sa.Column("mismatch_evidence", sa.String(20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("page_intent_profiles", "mismatch_evidence")
    op.drop_column("page_intent_profiles", "content_hash")
    op.drop_column("page_intent_profiles", "page_type")
    op.drop_index("ix_rec_score_status", table_name="recommendation_scores")
    op.drop_index("ix_rec_score_website_level", table_name="recommendation_scores")
    op.drop_index("ix_rec_score_website_priority", table_name="recommendation_scores")
    op.drop_index("ix_recommendation_scores_page_id", table_name="recommendation_scores")
    op.drop_index("ix_recommendation_scores_website_id", table_name="recommendation_scores")
    op.drop_table("recommendation_scores")
