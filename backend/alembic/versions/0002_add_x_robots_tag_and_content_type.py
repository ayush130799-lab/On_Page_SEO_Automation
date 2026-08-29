"""Add x_robots_tag and content_type columns to pages table.

Revision ID: 0002_x_robots_tag
Revises: 0001_initial
Create Date: 2026-08-29 03:44:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = '0002_x_robots_tag'
down_revision: str | None = '0001_initial'
branch_labels: str | None = None
depends_on: str | None = None

def upgrade() -> None:
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS x_robots_tag TEXT;")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS content_type VARCHAR(255);")

def downgrade() -> None:
    op.drop_column('pages', 'content_type')
    op.drop_column('pages', 'x_robots_tag')
