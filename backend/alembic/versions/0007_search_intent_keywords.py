"""Phase 2: Search Intent Detection & Keyword Intelligence tables.

Adds two new tables:
  - ``page_intent_profiles`` — one record per page, upserted on each analysis run.
    Stores the detected search intent, confidence, mismatch flags, keyword tier arrays,
    and the overall keyword opportunity score.
  - ``keyword_opportunities`` — one row per keyword per tier, keyed to a profile.
    Replaced wholesale when the profile refreshes.

Revision ID: 0007_search_intent_keywords
Revises: 0006_content_captured_at
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_search_intent_keywords"
down_revision = "0006_content_captured_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── page_intent_profiles ────────────────────────────────────────────────
    op.create_table(
        "page_intent_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("crawl_run_id", sa.Integer(), nullable=True),
        # timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # intent classification
        sa.Column("detected_intent", sa.String(32), nullable=True),
        sa.Column("intent_confidence", sa.Float(), nullable=True),
        sa.Column("detection_method", sa.String(20), nullable=True),
        sa.Column("business_intent", sa.String(32), nullable=True),
        # mismatch
        sa.Column(
            "intent_mismatch",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("mismatch_severity", sa.String(4), nullable=True),
        sa.Column("mismatch_explanation", sa.Text(), nullable=True),
        # keyword tier arrays (JSONB on Postgres, JSON elsewhere)
        sa.Column("primary_keywords", sa.JSON(), nullable=True),
        sa.Column("secondary_keywords", sa.JSON(), nullable=True),
        sa.Column("long_tail_keywords", sa.JSON(), nullable=True),
        sa.Column("semantic_entities", sa.JSON(), nullable=True),
        sa.Column("question_keywords", sa.JSON(), nullable=True),
        # aggregate opportunity score
        sa.Column("keyword_opportunity_score", sa.Float(), nullable=True),
        sa.Column("analysed_at", sa.DateTime(timezone=True), nullable=True),
        # constraints
        sa.ForeignKeyConstraint(["crawl_run_id"], ["crawl_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_id", name="uq_intent_profile_page"),
    )
    op.create_index("ix_intent_profile_website", "page_intent_profiles", ["website_id"])
    op.create_index(
        "ix_intent_profile_mismatch",
        "page_intent_profiles",
        ["website_id", "intent_mismatch"],
    )
    op.create_index(
        "ix_intent_profile_intent",
        "page_intent_profiles",
        ["website_id", "detected_intent"],
    )
    op.create_index("ix_page_intent_profiles_page_id", "page_intent_profiles", ["page_id"])

    # ── keyword_opportunities ───────────────────────────────────────────────
    op.create_table(
        "keyword_opportunities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("intent_profile_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("keyword", sa.String(255), nullable=False),
        sa.Column("keyword_tier", sa.String(20), nullable=False),
        # sub-scores
        sa.Column("demand_score", sa.Float(), nullable=True),
        sa.Column("ranking_opportunity_score", sa.Float(), nullable=True),
        sa.Column("intent_match_score", sa.Float(), nullable=True),
        sa.Column("business_relevance_score", sa.Float(), nullable=True),
        sa.Column("content_relevance_score", sa.Float(), nullable=True),
        sa.Column("competition_opportunity_score", sa.Float(), nullable=True),
        sa.Column("keyword_opportunity_score", sa.Float(), nullable=True),
        # existing data signals
        sa.Column("current_position", sa.Float(), nullable=True),
        sa.Column("current_impressions", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(20), nullable=True),
        sa.Column("extra_data", sa.JSON(), nullable=True),
        # constraints
        sa.ForeignKeyConstraint(
            ["intent_profile_id"], ["page_intent_profiles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kw_opp_profile", "keyword_opportunities", ["intent_profile_id"])
    op.create_index(
        "ix_kw_opp_website_tier", "keyword_opportunities", ["website_id", "keyword_tier"]
    )
    op.create_index("ix_keyword_opportunities_page_id", "keyword_opportunities", ["page_id"])


def downgrade() -> None:
    op.drop_table("keyword_opportunities")
    op.drop_table("page_intent_profiles")
