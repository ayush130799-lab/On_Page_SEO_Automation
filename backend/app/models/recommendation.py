"""Structured AI recommendations produced by the LLM stage of the pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .page import Page


class AIRecommendation(TimestampMixin, Base):
    """One LLM analysis of one page.

    The full validated JSON payload is kept in ``payload``; the commonly-queried fields are also
    promoted to columns so the dashboard can sort and filter without deserialising every row.
    """

    __tablename__ = "ai_recommendations"
    __table_args__ = (
        Index("ix_ai_rec_page_created", "page_id", "created_at"),
        Index("ix_ai_rec_website_priority", "website_id", "priority"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seo_audit_id: Mapped[int | None] = mapped_column(
        ForeignKey("seo_audits.id", ondelete="SET NULL")
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )

    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="completed", nullable=False)

    # ── Promoted summary fields ─────────────────────────────────────────────
    summary: Mapped[str | None] = mapped_column(Text)
    search_intent: Mapped[str | None] = mapped_column(String(100))
    priority: Mapped[str | None] = mapped_column(String(20))
    confidence: Mapped[float | None] = mapped_column(Float)
    expected_impact: Mapped[str | None] = mapped_column(Text)
    content_quality_score: Mapped[float | None] = mapped_column(Float)
    topic_coverage_score: Mapped[float | None] = mapped_column(Float)

    # Dual-impact and explainability fields (Phase 1)
    search_impact_score: Mapped[float | None] = mapped_column(Float)
    user_activity_score: Mapped[float | None] = mapped_column(Float)
    impact_score: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)

    suggested_title: Mapped[str | None] = mapped_column(Text)
    suggested_meta_description: Mapped[str | None] = mapped_column(Text)

    #: The complete validated response (findings, suggested changes, implementation guidance).
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    #: Content hash at analysis time — a matching hash means the cached result is still valid.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    seo_score_at_analysis: Mapped[float | None] = mapped_column(Float)
    priority_score_at_analysis: Mapped[float | None] = mapped_column(Float)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    page: Mapped["Page"] = relationship(back_populates="recommendations")
