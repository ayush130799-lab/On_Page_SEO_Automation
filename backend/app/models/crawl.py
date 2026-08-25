"""Crawl runs — one execution of the discovery + fetch + audit pipeline for a website."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime
from .enums import CrawlMode, CrawlTrigger, RunStatus

if TYPE_CHECKING:
    from .audit import SEOAudit
    from .website import Website


class CrawlRun(TimestampMixin, Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        Index("ix_crawl_runs_website_created", "website_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(30), default=RunStatus.QUEUED, nullable=False, index=True
    )
    trigger: Mapped[str] = mapped_column(String(30), default=CrawlTrigger.MANUAL, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default=CrawlMode.FULL, nullable=False)
    #: For incremental runs: the exact URLs requested (e.g. from a GitHub push).
    target_urls: Mapped[list[str] | None] = mapped_column(JSONColumn)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    #: Set when a GitHub push triggered this run. Intentionally *not* a foreign key: the
    #: authoritative link is ``github_events.crawl_run_id``, and declaring both directions would
    #: create a circular constraint that no migration ordering can satisfy.
    github_event_id: Mapped[int | None] = mapped_column(Integer, index=True)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    # ── Progress counters (polled by the dashboard) ─────────────────────────
    urls_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_queued: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_rendered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_analysed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Free-text stage label: "discovering", "crawling", "auditing", "scoring", "ai".
    stage: Mapped[str | None] = mapped_column(String(40))

    average_seo_score: Mapped[float | None] = mapped_column(Float)
    critical_issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error: Mapped[str | None] = mapped_column(Text)
    #: Snapshot of the effective crawl configuration, for reproducibility.
    config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    website: Mapped["Website"] = relationship(back_populates="crawl_runs")
    audits: Mapped[list["SEOAudit"]] = relationship(
        back_populates="crawl_run", cascade="all, delete-orphan"
    )
