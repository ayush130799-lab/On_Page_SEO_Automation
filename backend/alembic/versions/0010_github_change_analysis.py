"""Step 4: GitHub Change Analysis — roadmap §8 / §11.

Four tables: ``github_pull_requests``, ``github_commits``, ``github_changes``,
``deployment_analyses``. ``github_repositories`` from §11's list is deliberately not created —
this codebase already models "one repository per website" on ``Website``
(github_repo/github_branch/github_framework) and its GitHub ``Integration`` row (credentials,
config); a second table holding the same facts would only be a second place for them to drift out
of sync. See the module docstring on ``app.models.github_analysis`` for the full reasoning.

Note: the ``legacy_*`` tables preserved by migration 0001 are intentionally absent from the ORM
metadata. Autogenerate proposes dropping them on every revision; that must never be accepted.

Revision ID: 0010_github_change_analysis
Revises: 0009_seo_roadmaps
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import UTCDateTime

revision: str = "0010_github_change_analysis"
down_revision: str | None = "0009_seo_roadmaps"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_pull_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("state", sa.String(10), server_default="open", nullable=False),
        sa.Column("base_branch", sa.String(255), nullable=True),
        sa.Column("head_branch", sa.String(255), nullable=True),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("html_url", sa.String(500), nullable=True),
        sa.Column("opened_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("closed_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("merged_at", UTCDateTime(timezone=True), nullable=True),
        sa.Column("analysis_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", UTCDateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("website_id", "number", name="uq_github_pr_website_number"),
    )
    op.create_index("ix_github_pull_requests_website_id", "github_pull_requests", ["website_id"])
    op.create_index(
        "ix_github_pr_website_state", "github_pull_requests", ["website_id", "state"]
    )

    op.create_table(
        "github_commits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("pull_request_id", sa.Integer(), nullable=True),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("author_name", sa.String(255), nullable=True),
        sa.Column("author_login", sa.String(255), nullable=True),
        sa.Column("committed_at", UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["pull_request_id"], ["github_pull_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_github_commits_website", "github_commits", ["website_id"])
    op.create_index("ix_github_commits_pr", "github_commits", ["pull_request_id"])

    op.create_table(
        "deployment_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("pull_request_id", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("positive_confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("negative_confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("expected_impact", sa.String(10), nullable=False),
        sa.Column("risk_level", sa.String(10), nullable=False),
        sa.Column("positive_findings", sa.JSON(), nullable=False),
        sa.Column("negative_findings", sa.JSON(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("suggested_changes", sa.JSON(), nullable=False),
        sa.Column("comment_body", sa.Text(), nullable=True),
        sa.Column("comment_posted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("comment_error", sa.Text(), nullable=True),
        sa.Column("gate_mode", sa.String(10), server_default="off", nullable=False),
        sa.Column("gate_status_posted", sa.String(10), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["pull_request_id"], ["github_pull_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deployment_analyses_website", "deployment_analyses", ["website_id"])
    op.create_index("ix_deployment_analyses_pr", "deployment_analyses", ["pull_request_id"])

    op.create_table(
        "github_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("website_id", sa.Integer(), nullable=False),
        sa.Column("deployment_analysis_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("affected_url", sa.String(2048), nullable=True),
        sa.Column("change_type", sa.String(30), nullable=False),
        sa.Column("before_value", sa.Text(), nullable=True),
        sa.Column("after_value", sa.Text(), nullable=True),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("weight", sa.Float(), server_default="0.5", nullable=False),
        sa.Column(
            "extraction_method", sa.String(20), server_default="diff_heuristic", nullable=False
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["website_id"], ["websites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["deployment_analysis_id"], ["deployment_analyses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_github_changes_deployment_analysis", "github_changes", ["deployment_analysis_id"])
    op.create_index("ix_github_changes_website", "github_changes", ["website_id"])


def downgrade() -> None:
    op.drop_index("ix_github_changes_website", table_name="github_changes")
    op.drop_index("ix_github_changes_deployment_analysis", table_name="github_changes")
    op.drop_table("github_changes")

    op.drop_index("ix_deployment_analyses_pr", table_name="deployment_analyses")
    op.drop_index("ix_deployment_analyses_website", table_name="deployment_analyses")
    op.drop_table("deployment_analyses")

    op.drop_index("ix_github_commits_pr", table_name="github_commits")
    op.drop_index("ix_github_commits_website", table_name="github_commits")
    op.drop_table("github_commits")

    op.drop_index("ix_github_pr_website_state", table_name="github_pull_requests")
    op.drop_index("ix_github_pull_requests_website_id", table_name="github_pull_requests")
    op.drop_table("github_pull_requests")
