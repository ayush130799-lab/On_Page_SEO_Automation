"""Stable page identity.

A ``Page`` belongs to a **website**, not to a crawl. This is what makes history possible: audits,
metrics, priority scores and AI recommendations all accumulate against a row that survives every
re-crawl. ``url_hash`` (SHA-256 of the normalised URL) keeps the uniqueness index small and fast
even for very long URLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime
from .enums import AIStatus

if TYPE_CHECKING:
    from .audit import SEOAudit, SEOIssue
    from .intent import PageIntentProfile
    from .metrics import GA4Metric, GSCMetric, HistoricalMetric, SemrushMetric
    from .priority import PriorityScore
    from .recommendation import AIRecommendation
    from .recommendation_score import RecommendationScore
    from .website import Website


class Page(TimestampMixin, Base):
    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("website_id", "url_hash", name="uq_page_website_url"),
        # Dashboard default ordering: highest business priority first.
        Index("ix_pages_website_priority", "website_id", "priority_score"),
        Index("ix_pages_website_seo_score", "website_id", "seo_score"),
        Index("ix_pages_website_active", "website_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Normalised path used for GSC/GA4 matching and file→page mapping ("/", "/blog/post").
    path: Mapped[str] = mapped_column(String(1024), index=True, nullable=False, default="/")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_crawled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: When the stored content signals were captured. Older than ``last_crawled_at`` means
    #: the most recent crawl carried no document and the previous values were kept rather
    #: than being overwritten with blanks.
    content_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # ── Latest crawl snapshot (denormalised for fast list queries) ──────────
    status_code: Mapped[int | None] = mapped_column(Integer, index=True)
    final_url: Mapped[str | None] = mapped_column(String(2048))
    redirect_chain: Mapped[list[str] | None] = mapped_column(JSONColumn)
    title: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    h1: Mapped[str | None] = mapped_column(Text)
    h1_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    h4_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    h5_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    h6_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    empty_heading_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: How many <title> / <meta name="description"> / <meta name="robots"> tags were present.
    #: More than one is a conflict search engines resolve arbitrarily.
    title_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta_description_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta_robots_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: missing | empty | self | other | relative | invalid | multiple
    canonical_status: Mapped[str | None] = mapped_column(String(20))
    #: Three documented word measurements - see crawler/extractor.py.
    raw_word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    visible_word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    main_content_word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: "main" | "article" | "body" - which subtree the content measure came from.
    content_scope: Mapped[str | None] = mapped_column(String(20))
    tracking_pixel_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    non_http_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sponsored_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ugc_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    structured_data_formats: Mapped[list[str] | None] = mapped_column(JSONColumn)
    #: Set when JavaScript rendering was required but did not succeed.
    render_error: Mapped[str | None] = mapped_column(Text)
    extraction_errors: Mapped[list[str] | None] = mapped_column(JSONColumn)
    h2_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    h3_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    #: Raw href value from the canonical tag before URL resolution (e.g. "/about/" vs resolved).
    canonical_raw: Mapped[str | None] = mapped_column(Text)
    #: Number of <link rel="canonical"> tags found; >1 = conflict.
    canonical_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    robots_directive: Mapped[str | None] = mapped_column(Text)
    x_robots_tag: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(255))
    lang: Mapped[str | None] = mapped_column(String(20))
    hreflang: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONColumn)
    has_viewport: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_structured_data: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    structured_data_types: Mapped[list[str] | None] = mapped_column(JSONColumn)
    has_open_graph: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    #: SHA-256 of the extracted text — drives duplicate detection and the AI cache.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Images where the alt attribute is completely absent (genuine oversight).
    missing_alt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Images where alt="" (intentionally decorative — correct accessibility practice).
    empty_alt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    internal_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    external_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    broken_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: How many other pages on this site link here — 0 means orphan.
    inbound_internal_links: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Links with rel=sponsored.
    sponsored_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Links with rel=ugc (user generated content).
    ugc_link_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Pagination signals extracted from <link rel="next/prev">.
    pagination_next: Mapped[str | None] = mapped_column(String(2048))
    pagination_prev: Mapped[str | None] = mapped_column(String(2048))

    was_rendered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    content_bytes: Mapped[int | None] = mapped_column(Integer)
    crawl_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    crawl_error: Mapped[str | None] = mapped_column(Text)
    #: Data quality indicator — separates extraction failures from SEO issues.
    #: "ok" | "partial" | "render_failed" | "failed"
    crawl_quality: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)

    # ── Latest scores (denormalised; authoritative rows live in seo_audits /
    #    priority_scores) ────────────────────────────────────────────────────
    seo_score: Mapped[float | None] = mapped_column(Float)
    seo_category: Mapped[str | None] = mapped_column(String(30), index=True)
    highest_severity: Mapped[str | None] = mapped_column(String(20), index=True)
    issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priority_score: Mapped[float | None] = mapped_column(Float)
    priority_band: Mapped[str | None] = mapped_column(String(10), index=True)
    priority_rank: Mapped[int | None] = mapped_column(Integer)

    ai_status: Mapped[str] = mapped_column(
        String(30), default=AIStatus.PENDING, nullable=False, index=True
    )
    ai_analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # ── Relationships ───────────────────────────────────────────────────────
    website: Mapped["Website"] = relationship(back_populates="pages")
    audits: Mapped[list["SEOAudit"]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="SEOAudit.id.desc()"
    )
    issues: Mapped[list["SEOIssue"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    gsc_metrics: Mapped[list["GSCMetric"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    ga4_metrics: Mapped[list["GA4Metric"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    semrush_metrics: Mapped[list["SemrushMetric"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    priority_scores: Mapped[list["PriorityScore"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    recommendation_scores: Mapped[list["RecommendationScore"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    recommendations: Mapped[list["AIRecommendation"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    historical_metrics: Mapped[list["HistoricalMetric"]] = relationship(
        back_populates="page", cascade="all, delete-orphan"
    )
    intent_profile: Mapped["PageIntentProfile | None"] = relationship(
        back_populates="page", cascade="all, delete-orphan", uselist=False
    )
