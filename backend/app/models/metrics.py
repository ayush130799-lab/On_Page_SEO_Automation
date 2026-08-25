"""Normalised, page-level metrics from GSC, GA4 and Semrush, plus daily historical rollups.

Every metric table is keyed ``(page_id, date)`` and upserted, so a re-sync of an overlapping window
is idempotent. Keeping one row per day (rather than a single "current" row) is what makes the
history charts and trend comparisons possible.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin

if TYPE_CHECKING:
    from .page import Page
    from .website import Website


class GSCMetric(TimestampMixin, Base):
    """Google Search Console search-analytics data for one page on one day."""

    __tablename__ = "gsc_metrics"
    __table_args__ = (
        UniqueConstraint("page_id", "date", name="uq_gsc_page_date"),
        Index("ix_gsc_page_date", "page_id", "date"),
        Index("ix_gsc_website_date", "website_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False)

    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ctr: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    position: Mapped[float | None] = mapped_column(Float)
    #: Top queries for this page/date:
    #: ``[{"query": "...", "clicks": 12, "impressions": 300, "ctr": .04, "position": 8.1}]``
    queries: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONColumn)

    page: Mapped["Page"] = relationship(back_populates="gsc_metrics")


class GA4Metric(TimestampMixin, Base):
    """Google Analytics 4 engagement and conversion data for one page on one day."""

    __tablename__ = "ga4_metrics"
    __table_args__ = (
        UniqueConstraint("page_id", "date", name="uq_ga4_page_date"),
        Index("ix_ga4_page_date", "page_id", "date"),
        Index("ix_ga4_website_date", "website_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False)

    users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    screen_page_views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engaged_sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engagement_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    average_engagement_time: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    bounce_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    conversions: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Purchase revenue where available, otherwise total event value.
    revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(10))

    page: Mapped["Page"] = relationship(back_populates="ga4_metrics")


class SemrushMetric(TimestampMixin, Base):
    """Semrush organic visibility, keyword opportunity and backlink data for one page."""

    __tablename__ = "semrush_metrics"
    __table_args__ = (
        UniqueConstraint("page_id", "date", name="uq_semrush_page_date"),
        Index("ix_semrush_page_date", "page_id", "date"),
        Index("ix_semrush_website_date", "website_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    database: Mapped[str | None] = mapped_column(String(10))

    organic_keywords: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    organic_traffic: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    organic_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Keywords ranking 4-20 — the "striking distance" opportunity band.
    striking_distance_keywords: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Aggregate monthly search volume across striking-distance keywords.
    opportunity_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    best_position: Mapped[float | None] = mapped_column(Float)
    average_position: Mapped[float | None] = mapped_column(Float)
    backlinks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    referring_domains: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: ``[{"keyword": "...", "position": 12, "volume": 2400, "cpc": 3.1, "difficulty": 55}]``
    keywords: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONColumn)

    page: Mapped["Page"] = relationship(back_populates="semrush_metrics")


class HistoricalMetric(TimestampMixin, Base):
    """Daily rollup of derived scores, at page or website scope.

    Provider metric tables have retention windows and can be re-synced; this table is written by
    the platform itself and is therefore the durable record of how scores moved over time.
    """

    __tablename__ = "historical_metrics"
    __table_args__ = (
        UniqueConstraint("website_id", "page_id", "date", "scope", name="uq_historical_point"),
        Index("ix_historical_website_date", "website_id", "date"),
        Index("ix_historical_page_date", "page_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: NULL for website-scope rows.
    page_id: Mapped[int | None] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"))
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="page", nullable=False)

    seo_score: Mapped[float | None] = mapped_column(Float)
    priority_score: Mapped[float | None] = mapped_column(Float)
    issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    critical_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conversions: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    revenue: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    page: Mapped["Page | None"] = relationship(back_populates="historical_metrics")
    website: Mapped["Website"] = relationship()
