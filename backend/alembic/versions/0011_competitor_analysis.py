"""Live SERP competitor analysis — roadmap §4.2 / §7.4, the ``competitor_data`` table from §11.

Two tables: ``competitor_analyses`` (one row per keyword lookup — the SERP-level facts: PAA
questions, related searches, and the content-gap summary against the page being analysed) and
``competitor_results`` (one row per top-N competitor URL actually fetched, with word count and
heading structure measured by the crawler's own extractor).

Note: the ``legacy_*`` tables preserved by migration 0001 are intentionally absent from the ORM
metadata. Autogenerate proposes dropping them on every revision; that must never be accepted.

Revision ID: 0011_competitor_analysis
Revises: 0010_github_change_analysis
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import UTCDateTime

revision: str = "0011_competitor_analysis"
down_revision: str | None = "0010_github_change_analysis"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "competitor_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=True),
        sa.Column("keyword", sa.String(255), nullable=False),
        sa.Column("search_location", sa.String(10), nullable=True),
        sa.Column("search_language", sa.String(10), nullable=True),
        sa.Column("paa_questions", sa.JSON(), nullable=False),
        sa.Column("related_searches", sa.JSON(), nullable=False),
        sa.Column("this_page_word_count", sa.Integer(), nullable=True),
        sa.Column("competitor_median_word_count", sa.Integer(), nullable=True),
        sa.Column("competitor_avg_h2_count", sa.Float(), nullable=True),
        sa.Column("missing_subtopics", sa.JSON(), nullable=False),
        sa.Column("fetched_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("analysed_at", UTCDateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_competitor_analyses_website_id", "competitor_analyses", ["website_id"])
    op.create_index("ix_competitor_analyses_page_id", "competitor_analyses", ["page_id"])

    op.create_table(
        "competitor_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("competitor_analysis_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("fetch_status", sa.String(10), server_default="ok", nullable=False),
        sa.Column("fetch_error", sa.Text(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("h1_text", sa.Text(), nullable=True),
        sa.Column("h1_count", sa.Integer(), nullable=True),
        sa.Column("h2_count", sa.Integer(), nullable=True),
        sa.Column("h3_count", sa.Integer(), nullable=True),
        sa.Column("headings", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["competitor_analysis_id"], ["competitor_analyses.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_competitor_results_competitor_analysis_id", "competitor_results",
        ["competitor_analysis_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_competitor_results_competitor_analysis_id", table_name="competitor_results")
    op.drop_table("competitor_results")

    op.drop_index("ix_competitor_analyses_page_id", table_name="competitor_analyses")
    op.drop_index("ix_competitor_analyses_website_id", table_name="competitor_analyses")
    op.drop_table("competitor_analyses")
