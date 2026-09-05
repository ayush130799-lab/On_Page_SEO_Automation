"""Per-recommendation impact scores — roadmap §11.1.

``AIRecommendation`` is one row per *page*: the raw LLM response, kept as an audit trail. That
shape cannot express §4.4, which requires a separate score for each recommendation on a page
("rewrite title 94, improve CTA 91, add alt text 31"). This table is that missing grain: one row
per page per recommendation type, holding both objective scores separately, the business score,
the blended priority, the confidence, and — per §9.1 — the human-readable reason and expected
outcome that must accompany any number the UI shows.

Rows are replaced for a page whenever it is re-scored, so the table always reflects the latest
crawl rather than accumulating history. Historical tracking belongs to ``seo_experiments`` (§8.4)
where predicted and actual outcomes are compared.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .page import Page


#: §11.1 lifecycle. Distinct from the job status on ``AIRecommendation``, which describes whether
#: the LLM call completed — this describes what a human decided to do about the finding.
RECOMMENDATION_STATUSES = (
    "detected",
    "approved",
    "in_progress",
    "implemented",
    "rejected",
    "validated",
    "failed",
)

PRIORITY_LEVELS = ("P0", "P1", "P2", "P3")


class RecommendationScore(TimestampMixin, Base):
    """One scored recommendation for one page."""

    __tablename__ = "recommendation_scores"
    __table_args__ = (
        UniqueConstraint("page_id", "recommendation_type", name="uq_rec_score_page_type"),
        Index("ix_rec_score_website_priority", "website_id", "overall_priority"),
        Index("ix_rec_score_website_level", "website_id", "priority_level"),
        Index("ix_rec_score_status", "website_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )
    #: The page-level LLM analysis this recommendation's wording came from, when it had one.
    ai_recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_recommendations.id", ondelete="SET NULL")
    )

    # ── What is being recommended ───────────────────────────────────────────
    #: Matches a key in services.impact.catalog (usually a rule ``check_type``).
    recommendation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    current_state: Mapped[str | None] = mapped_column(Text)
    recommended_state: Mapped[str | None] = mapped_column(Text)

    # ── Keyword / intent context ────────────────────────────────────────────
    primary_keyword: Mapped[str | None] = mapped_column(String(255))
    secondary_keywords: Mapped[list[str] | None] = mapped_column(JSONColumn)
    search_intent: Mapped[str | None] = mapped_column(String(32))

    # ── §4.4: two objectives, never collapsed into one ──────────────────────
    search_impact_score: Mapped[float | None] = mapped_column(Float)
    user_activity_score: Mapped[float | None] = mapped_column(Float)
    business_impact_score: Mapped[float | None] = mapped_column(Float)
    overall_priority: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float | None] = mapped_column(Float)

    #: P0 | P1 | P2 | P3 — §7.2 band derived from the scores and the P0 criteria.
    priority_level: Mapped[str | None] = mapped_column(String(4))
    severity: Mapped[str | None] = mapped_column(String(20))
    effort: Mapped[str | None] = mapped_column(String(10))

    # ── §9.1: a number is never shown without its explanation ───────────────
    reason: Mapped[str | None] = mapped_column(Text)
    expected_outcome: Mapped[str | None] = mapped_column(Text)

    #: Which cost tier produced this row — rules | statistical | ai | deep_ai.
    tier: Mapped[str | None] = mapped_column(String(20))
    #: Every factor value and weight that produced the scores, so a figure can be traced.
    factors: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    # ── Lifecycle ───────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(20), default="detected", nullable=False)
    scored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    page: Mapped["Page"] = relationship(back_populates="recommendation_scores")
