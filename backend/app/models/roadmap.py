"""The generated Website SEO Roadmap — roadmap §7.3 and §11.

A roadmap is a snapshot: the ranked recommendations that existed at generation time, grouped into
weekly sprints. It is regenerated on demand rather than kept continuously in sync with
``recommendation_scores`` (which changes on every crawl and every nightly rescore) — a roadmap the
team is actively working from should not silently reshuffle under them mid-week. ``weeks`` is
self-contained JSON (recommendation ids, urls, titles, scores, reasons) so the sprint plan remains
readable even after the underlying recommendation rows are later replaced by a re-score.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .website import Website


class SeoRoadmap(TimestampMixin, Base):
    """One generated roadmap for a website."""

    __tablename__ = "seo_roadmaps"
    __table_args__ = (
        Index("ix_seo_roadmaps_website_generated", "website_id", "generated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )

    generated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    # ── §10.1 website overview, snapshotted at generation time ──────────────
    overall_seo_opportunity: Mapped[float | None] = mapped_column(Float)
    #: LOW | MEDIUM | HIGH
    organic_growth_opportunity: Mapped[str | None] = mapped_column(String(10))
    user_activity_opportunity: Mapped[str | None] = mapped_column(String(10))
    critical_issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    high_impact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    medium_impact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_impact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: §7.3 — ``[{"week": 1, "label": "...", "items": [{...}, ...]}, ...]``
    weeks: Mapped[list[dict[str, Any]]] = mapped_column(JSONColumn, nullable=False)
    #: §7.1 — the full per-page priority matrix at generation time.
    priority_matrix: Mapped[list[dict[str, Any]]] = mapped_column(JSONColumn, nullable=False)

    website: Mapped["Website"] = relationship(back_populates="roadmaps")
