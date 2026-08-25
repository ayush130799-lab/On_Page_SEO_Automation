"""Computed business-priority scores.

Kept deliberately separate from ``SEOAudit.seo_score``: one answers *"how healthy is this page?"*,
the other *"how much does fixing it matter?"*. Every row stores both the component values and the
weight vector that produced the result, so a score computed months ago remains explainable after
the weights are retuned.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .page import Page


class PriorityScore(TimestampMixin, Base):
    __tablename__ = "priority_scores"
    __table_args__ = (
        Index("ix_priority_page_computed", "page_id", "computed_at"),
        Index("ix_priority_website_score", "website_id", "score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: Final 0-100 business priority.
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: P0-P3 band derived from the score distribution across the website.
    band: Mapped[str | None] = mapped_column(String(10), index=True)
    rank: Mapped[int | None] = mapped_column(Integer)

    # ── Normalised component values (0-1) ───────────────────────────────────
    seo_severity_component: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ga4_activity_component: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    gsc_search_component: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    semrush_opportunity_component: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )

    #: Effective weights after redistribution for missing integrations.
    weights: Mapped[dict[str, float] | None] = mapped_column(JSONColumn)
    #: Raw inputs behind each component — what the UI shows in the "why" panel.
    breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    #: Which providers actually contributed, e.g. ``["seo", "ga4", "gsc"]``.
    data_sources: Mapped[list[str] | None] = mapped_column(JSONColumn)
    metric_window_days: Mapped[int | None] = mapped_column(Integer)

    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    page: Mapped["Page"] = relationship(back_populates="priority_scores")
