"""Add Traffic Potential and Lead Potential scores to pages.

Two 0-100 opportunity scores, denormalised onto ``pages`` alongside ``seo_score`` and
``priority_score`` for fast dashboard reads. Computed in ``services/opportunity_scoring.py``
from data already collected (GSC/GA4 aggregates, the keyword/intent engine's outputs) and
recomputed as part of intent analysis — existing rows are simply ``NULL`` until the next run.

Revision ID: 0013_opportunity_scores
Revises: 0012_seo_experiments
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_opportunity_scores"
down_revision: str | None = "0012_seo_experiments"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("pages", sa.Column("traffic_potential_score", sa.Float(), nullable=True))
    op.add_column("pages", sa.Column("lead_potential_score", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("pages", "lead_potential_score")
    op.drop_column("pages", "traffic_potential_score")
