"""Received GitHub webhook deliveries and what the platform did with them."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .website import Website


class GitHubEvent(TimestampMixin, Base):
    """A verified webhook delivery.

    ``delivery_id`` is unique, which makes redeliveries idempotent — GitHub retries aggressively and
    a duplicate must never trigger a second re-audit.
    """

    __tablename__ = "github_events"
    __table_args__ = (
        Index("ix_github_events_website_created", "website_id", "created_at"),
        Index("ix_github_events_repo", "repository"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: NULL when the repository does not map to a known website (still recorded, for diagnostics).
    website_id: Mapped[int | None] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )

    delivery_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    repository: Mapped[str | None] = mapped_column(String(255))
    branch: Mapped[str | None] = mapped_column(String(255))
    before_sha: Mapped[str | None] = mapped_column(String(64))
    after_sha: Mapped[str | None] = mapped_column(String(64))
    pusher: Mapped[str | None] = mapped_column(String(255))
    commit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    commit_messages: Mapped[list[str] | None] = mapped_column(JSONColumn)

    changed_files: Mapped[list[str] | None] = mapped_column(JSONColumn)
    changed_file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: URLs resolved from the changed files, when mapping succeeded.
    affected_urls: Mapped[list[str] | None] = mapped_column(JSONColumn)

    #: "incremental_crawl", "full_crawl", "ignored", "unmatched_repository", "error".
    action_taken: Mapped[str | None] = mapped_column(String(50), index=True)
    action_reason: Mapped[str | None] = mapped_column(Text)
    crawl_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(Text)

    #: Trimmed payload (secrets and large diffs removed) retained for debugging.
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    website: Mapped["Website | None"] = relationship(back_populates="github_events")
