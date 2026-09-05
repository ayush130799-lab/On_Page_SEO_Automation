"""Add crawler accuracy fields to pages table.

New columns support:
- Canonical accuracy: canonical_raw, canonical_count
- Alt text distinction: empty_alt_count (intentional decorative vs missing_alt_count which is genuine oversight)
- Pagination: pagination_next, pagination_prev
- Link attribution: sponsored_link_count, ugc_link_count
- Data quality: crawl_quality

Revision ID: 0003_crawler_accuracy
Revises: 0002_x_robots_tag
Create Date: 2026-09-02 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = '0003_crawler_accuracy'
down_revision: str | None = '0002_x_robots_tag'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Use ADD COLUMN IF NOT EXISTS for idempotency (PostgreSQL 9.6+)
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS canonical_raw TEXT;")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS canonical_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS empty_alt_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS sponsored_link_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS ugc_link_count INTEGER NOT NULL DEFAULT 0;")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS pagination_next VARCHAR(2048);")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS pagination_prev VARCHAR(2048);")
    op.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS crawl_quality VARCHAR(20) NOT NULL DEFAULT 'ok';")


def downgrade() -> None:
    for col in [
        "crawl_quality", "pagination_prev", "pagination_next",
        "ugc_link_count", "sponsored_link_count",
        "empty_alt_count", "canonical_count", "canonical_raw",
    ]:
        op.drop_column("pages", col)
