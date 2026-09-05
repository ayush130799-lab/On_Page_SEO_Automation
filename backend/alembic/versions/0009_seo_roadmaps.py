"""Step 3: seo_roadmaps table — roadmap §7.3 / §11.

A generated roadmap snapshot: the website overview (§10.1), the weekly sprint plan (§7.3), and the
full priority matrix (§7.1) at generation time, self-contained in JSON so a roadmap stays
readable after the underlying recommendation rows are later replaced by a re-score.

Note: the ``legacy_*`` tables preserved by migration 0001 are intentionally absent from the ORM
metadata. Autogenerate proposes dropping them on every revision; that must never be accepted.

Revision ID: 0009_seo_roadmaps
Revises: 0008_impact_scoring_engine
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import UTCDateTime

revision: str = "0009_seo_roadmaps"
down_revision: str | None = "0008_impact_scoring_engine"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "seo_roadmaps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("crawl_run_id", sa.Integer(), nullable=True),
        sa.Column("generated_at", UTCDateTime(timezone=True), nullable=False),
        # §10.1 website overview snapshot
        sa.Column("overall_seo_opportunity", sa.Float(), nullable=True),
        sa.Column("organic_growth_opportunity", sa.String(10), nullable=True),
        sa.Column("user_activity_opportunity", sa.String(10), nullable=True),
        sa.Column("critical_issue_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("high_impact_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("medium_impact_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("low_impact_count", sa.Integer(), server_default="0", nullable=False),
        # §7.3 weekly sprints, §7.1 priority matrix
        sa.Column("weeks", sa.JSON(), nullable=False),
        sa.Column("priority_matrix", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["crawl_run_id"], ["crawl_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_seo_roadmaps_website_id", "seo_roadmaps", ["website_id"])
    op.create_index(
        "ix_seo_roadmaps_website_generated", "seo_roadmaps", ["website_id", "generated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_seo_roadmaps_website_generated", table_name="seo_roadmaps")
    op.drop_index("ix_seo_roadmaps_website_id", table_name="seo_roadmaps")
    op.drop_table("seo_roadmaps")
