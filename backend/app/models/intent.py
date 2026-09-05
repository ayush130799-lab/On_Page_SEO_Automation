"""Search intent classification and keyword opportunity models (Phase 2).

``PageIntentProfile`` is the single authoritative intent record for a page.
It is upserted on every analysis run so the dashboard always shows the freshest
classification without accumulating stale rows.

``KeywordOpportunity`` stores individual keywords keyed to a profile, one row
per keyword per tier.  Rows are replaced wholesale when the profile is refreshed.
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

if TYPE_CHECKING:
    from .page import Page

# ---------------------------------------------------------------------------
# Valid values for intent / tier / severity fields
# ---------------------------------------------------------------------------

INTENT_VALUES = frozenset(
    {"informational", "navigational", "commercial", "transactional", "local"}
)

DETECTION_METHOD_VALUES = frozenset({"rules", "statistical", "ai"})

KEYWORD_TIER_VALUES = frozenset(
    {"primary", "secondary", "long_tail", "semantic", "question"}
)

#: §6.1 page types, the second classification axis alongside search intent.
PAGE_TYPE_VALUES = frozenset({"commercial", "informational", "hybrid"})

MISMATCH_SEVERITY_VALUES = frozenset({"P0", "P1", "P2", "P3"})


class PageIntentProfile(TimestampMixin, Base):
    """One intent classification record per page.

    The composite unique constraint means ``INSERT … ON CONFLICT DO UPDATE``
    (upsert) works cleanly — the previous profile is updated rather than
    accumulating duplicate rows.
    """

    __tablename__ = "page_intent_profiles"
    __table_args__ = (
        UniqueConstraint("page_id", name="uq_intent_profile_page"),
        Index("ix_intent_profile_website", "website_id"),
        Index("ix_intent_profile_mismatch", "website_id", "intent_mismatch"),
        Index("ix_intent_profile_intent", "website_id", "detected_intent"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL")
    )

    # ── Intent classification ───────────────────────────────────────────────
    #: informational | navigational | commercial | transactional | local
    detected_intent: Mapped[str | None] = mapped_column(String(32))
    intent_confidence: Mapped[float | None] = mapped_column(Float)
    #: rules | statistical | ai  — which level produced the final classification
    detection_method: Mapped[str | None] = mapped_column(String(20))

    #: Business intent inferred from URL patterns (Level-1 rule output used as
    #: the "expected" intent for mismatch comparison).
    business_intent: Mapped[str | None] = mapped_column(String(32))

    #: §6.1's second axis: commercial | informational | hybrid. Distinct from ``detected_intent``
    #: — a pricing guide is informational in structure and commercial in purpose, and collapsing
    #: the two axes is what makes a tool recommend "add more depth" to a checkout page.
    page_type: Mapped[str | None] = mapped_column(String(16))

    #: Content hash at classification time. A page whose content changed is re-analysed; without
    #: this the first classification was kept for ever.
    content_hash: Mapped[str | None] = mapped_column(String(64))

    # ── Mismatch ────────────────────────────────────────────────────────────
    intent_mismatch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: P0 | P1 | P2 | P3
    mismatch_severity: Mapped[str | None] = mapped_column(String(4))
    mismatch_explanation: Mapped[str | None] = mapped_column(Text)
    #: gsc_queries | page_targeting | none — which evidence produced the mismatch verdict.
    mismatch_evidence: Mapped[str | None] = mapped_column(String(20))

    # ── Keyword tiers (denormalised arrays for quick display) ───────────────
    primary_keywords: Mapped[list[str] | None] = mapped_column(JSONColumn)
    secondary_keywords: Mapped[list[str] | None] = mapped_column(JSONColumn)
    long_tail_keywords: Mapped[list[str] | None] = mapped_column(JSONColumn)
    semantic_entities: Mapped[list[str] | None] = mapped_column(JSONColumn)
    question_keywords: Mapped[list[str] | None] = mapped_column(JSONColumn)

    #: Composite score (0-100) measuring overall keyword opportunity for the page.
    keyword_opportunity_score: Mapped[float | None] = mapped_column(Float)

    analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # ── Relationships ───────────────────────────────────────────────────────
    page: Mapped["Page"] = relationship(back_populates="intent_profile", uselist=False)
    keyword_opportunities: Mapped[list["KeywordOpportunity"]] = relationship(
        back_populates="intent_profile", cascade="all, delete-orphan"
    )


class KeywordOpportunity(Base):
    """One keyword in one tier attached to a ``PageIntentProfile``.

    Rows are **replaced wholesale** when the profile is refreshed — the
    ``intent_profile_id`` FK cascade handles the delete.
    """

    __tablename__ = "keyword_opportunities"
    __table_args__ = (
        Index("ix_kw_opp_profile", "intent_profile_id"),
        Index("ix_kw_opp_website_tier", "website_id", "keyword_tier"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    intent_profile_id: Mapped[int] = mapped_column(
        ForeignKey("page_intent_profiles.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )

    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    #: primary | secondary | long_tail | semantic | question
    keyword_tier: Mapped[str] = mapped_column(String(20), nullable=False)

    # ── Opportunity sub-scores (0-100 each) ─────────────────────────────────
    demand_score: Mapped[float | None] = mapped_column(Float)
    ranking_opportunity_score: Mapped[float | None] = mapped_column(Float)
    intent_match_score: Mapped[float | None] = mapped_column(Float)
    business_relevance_score: Mapped[float | None] = mapped_column(Float)
    content_relevance_score: Mapped[float | None] = mapped_column(Float)
    competition_opportunity_score: Mapped[float | None] = mapped_column(Float)
    #: Composite: Demand x RankingOpp x IntentMatch x BusinessRel x ContentRel x CompetitionOpp
    keyword_opportunity_score: Mapped[float | None] = mapped_column(Float)

    # ── Known signals from existing integrations ────────────────────────────
    current_position: Mapped[float | None] = mapped_column(Float)
    current_impressions: Mapped[int | None] = mapped_column(Integer)
    #: gsc | ai | semrush
    source: Mapped[str | None] = mapped_column(String(20))

    # Extra context the AI returned (rationale etc.)
    extra_data: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)

    # ── Relationships ───────────────────────────────────────────────────────
    intent_profile: Mapped["PageIntentProfile"] = relationship(
        back_populates="keyword_opportunities"
    )
