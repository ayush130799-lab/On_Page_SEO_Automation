"""Record when a page's content signals were captured.

A crawl that fails to retrieve a document no longer overwrites the stored title, headings, word
count and link counts with blanks — those values are kept from the last successful crawl.
``content_captured_at`` makes their age explicit: when it is older than ``last_crawled_at``, the
most recent crawl carried no document and the content signals come from an earlier one.

Existing rows are backfilled from ``last_crawled_at``, which is when their content was in fact
captured under the previous behaviour.

Note: the ``legacy_*`` tables preserved by migration 0001 are intentionally absent from the ORM
metadata. Autogenerate proposes dropping them on every revision; that must never be accepted.

Revision ID: 0006_content_captured_at
Revises: 0005_crawler_accuracy_v2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.base import UTCDateTime

revision: str = "0006_content_captured_at"
down_revision: str | None = "0005_crawler_accuracy_v2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column("content_captured_at", UTCDateTime(timezone=True), nullable=True),
    )
    # Under the previous behaviour every crawl wrote content signals, so for existing rows the
    # capture time is the last crawl time.
    op.execute("UPDATE pages SET content_captured_at = last_crawled_at")


def downgrade() -> None:
    op.drop_column("pages", "content_captured_at")
