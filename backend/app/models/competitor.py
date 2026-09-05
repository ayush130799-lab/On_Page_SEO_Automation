"""Live SERP competitor analysis — roadmap §4.2 / §7.4 ("Competitor/SERP Analysis") and the
``competitor_data`` table from §11.

Two tables, mirroring the ``PageIntentProfile`` / ``KeywordOpportunity`` split already used for
Step 2's keyword tiers:

``CompetitorAnalysis``
    One row per (page, keyword, run) — the SERP-level facts: which questions Google's "People
    Also Ask" box surfaced, what related searches it suggested, and a summary of how this page
    compares to what is currently ranking.

``CompetitorResult``
    One row per competitor URL that was actually ranking and successfully fetched — its word
    count and heading structure, measured with the *same* extractor the site's own crawler uses,
    so "your page has 400 words, the average top-5 result has 1,650" is an apples-to-apples
    comparison rather than two different counting methods.

This is a paid, on-demand action (one SerpApi call plus up to
:data:`app.config.Settings.competitor_top_n` competitor page fetches) — triggered per page/keyword
from the UI, not run automatically across every page on every crawl. See
``app.services.serp`` for the fetch logic and cost reasoning.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONColumn, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .page import Page
    from .website import Website

FETCH_STATUSES = ("ok", "failed", "timeout", "blocked")


class CompetitorAnalysis(TimestampMixin, Base):
    """One SERP snapshot for one (page, keyword) pair."""

    __tablename__ = "competitor_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Nullable — an analysis can be run ad hoc for a keyword with no page attached yet.
    page_id: Mapped[int | None] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True
    )

    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The two-letter country / language SerpApi was queried with, so results stay comparable
    #: across re-runs even if global defaults change later.
    search_location: Mapped[str | None] = mapped_column(String(10))
    search_language: Mapped[str | None] = mapped_column(String(10))

    #: Google's "People Also Ask" box: [{"question": ..., "snippet": ...}, ...].
    paa_questions: Mapped[list[dict[str, Any]]] = mapped_column(JSONColumn, default=list, nullable=False)
    #: Related searches Google suggested for this query.
    related_searches: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)

    # ── This page vs. the field, computed once the competitor fetches complete ──────────────
    this_page_word_count: Mapped[int | None] = mapped_column(Integer)
    competitor_median_word_count: Mapped[int | None] = mapped_column(Integer)
    competitor_avg_h2_count: Mapped[float | None] = mapped_column(Float)
    #: Headings that appear on 2+ competitor pages but not on this one — the concrete subtopic
    #: gap, not just a word-count number.
    missing_subtopics: Mapped[list[str]] = mapped_column(JSONColumn, default=list, nullable=False)

    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    website: Mapped["Website"] = relationship()
    page: Mapped["Page | None"] = relationship()
    results: Mapped[list["CompetitorResult"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan",
        order_by="CompetitorResult.position",
    )


class CompetitorResult(Base):
    """One ranking competitor URL, with page metrics measured by the crawler's own extractor."""

    __tablename__ = "competitor_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_analyses.id", ondelete="CASCADE"), index=True, nullable=False
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)

    #: ok | failed | timeout | blocked
    fetch_status: Mapped[str] = mapped_column(String(10), default="ok", nullable=False)
    fetch_error: Mapped[str | None] = mapped_column(Text)

    # ── Measured with app.services.crawler.extractor — the same code path that measures the
    # user's own pages, so the comparison is methodologically apples-to-apples. ─────────────
    word_count: Mapped[int | None] = mapped_column(Integer)
    h1_text: Mapped[str | None] = mapped_column(Text)
    h1_count: Mapped[int | None] = mapped_column(Integer)
    h2_count: Mapped[int | None] = mapped_column(Integer)
    h3_count: Mapped[int | None] = mapped_column(Integer)
    #: [{"level": "h2", "text": "..."}, ...] — the actual subtopic structure, capped for storage.
    headings: Mapped[list[dict[str, str]]] = mapped_column(JSONColumn, default=list, nullable=False)

    analysis: Mapped["CompetitorAnalysis"] = relationship(back_populates="results")
