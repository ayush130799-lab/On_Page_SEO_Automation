"""Add impact scores and reason fields to ai_recommendations table.

New columns support:
- Dual-impact and explainability fields (Phase 1):
  search_impact_score, user_activity_score, impact_score, reason

Revision ID: 0004_ai_impact_scores
Revises: 0003_crawler_accuracy
Create Date: 2026-09-02 00:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = '0004_ai_impact_scores'
down_revision: str | None = '0003_crawler_accuracy'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS search_impact_score DOUBLE PRECISION;")
    op.execute("ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS user_activity_score DOUBLE PRECISION;")
    op.execute("ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS impact_score DOUBLE PRECISION;")
    op.execute("ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS reason TEXT;")


def downgrade() -> None:
    for col in ["reason", "impact_score", "user_activity_score", "search_impact_score"]:
        op.drop_column("ai_recommendations", col)
