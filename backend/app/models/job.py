"""Unified background-job tracking across every queue."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime
from .enums import RunStatus

if TYPE_CHECKING:
    from .website import Website


class Job(TimestampMixin, Base):
    """One tracked unit of background work (crawl, sync, scoring, AI, rollup).

    Celery already tracks task state, but that state is ephemeral and invisible to the dashboard.
    This table is the durable, queryable record the UI reads.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_website_created", "website_id", "created_at"),
        Index("ix_jobs_status_type", "status", "job_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int | None] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )

    job_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(30), default=RunStatus.QUEUED, nullable=False, index=True
    )
    queue: Mapped[str | None] = mapped_column(String(40))
    task_id: Mapped[str | None] = mapped_column(String(100), index=True)

    progress_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    progress_message: Mapped[str | None] = mapped_column(String(255))
    items_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Job arguments (never contains credentials — only ids and windows).
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    website: Mapped["Website | None"] = relationship(back_populates="jobs")
