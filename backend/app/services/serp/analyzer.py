"""Orchestrates one competitor analysis run: SERP lookup, competitor page fetches, persistence,
and the content-gap summary.

This is deliberately **on-demand**, not part of any automatic crawl or nightly rescore. SerpApi
bills per search and every competitor page fetch costs bandwidth and time; running it
unconditionally for every page's every keyword on a 10,000-page site would repeat exactly the
cost mistake Step 1 fixed for AI calls (§12.3). A human requests this for the specific page and
keyword they are working on, the same way they would open Ahrefs or Surfer for one URL at a time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import CompetitorAnalysis, CompetitorResult, Page, Website
from .client import SerpApiError, search
from .competitor_analyzer import CompetitorFetch, fetch_competitors

logger = logging.getLogger(__name__)


@dataclass
class CompetitorAnalysisOutcome:
    analysis_id: int | None = None
    keyword: str = ""
    fetched_count: int = 0
    failed_count: int = 0
    paa_count: int = 0
    error: str | None = None
    missing_subtopics: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gap_summary(
    this_page: Page | None, fetches: list[CompetitorFetch]
) -> tuple[int | None, int | None, float | None, list[str]]:
    """This page's word count vs. the field, and headings competitors cover that this page
    doesn't — the concrete, actionable half of "you're thin", not just a number."""
    ok = [f for f in fetches if f.fetch_status == "ok" and f.page is not None]
    if not ok:
        return (this_page.word_count if this_page else None), None, None, []

    word_counts = sorted(f.page.word_count for f in ok)  # type: ignore[union-attr]
    median_words = word_counts[len(word_counts) // 2]
    avg_h2 = sum(f.page.h2_count for f in ok) / len(ok)  # type: ignore[union-attr]

    # A subtopic (H2/H3 text) that at least two competitors independently cover is a real pattern,
    # not one site's idiosyncratic structure — that bar is what makes this a gap worth acting on.
    own_headings = set()
    if this_page is not None:
        # Only the H1 text is persisted on Page itself; deeper heading text isn't retained after
        # a crawl (see the Step 2 analyser's note on this same limitation), so the comparison
        # below is necessarily "what competitors cover" without a full own-page heading list.
        if this_page.h1:
            own_headings.add(this_page.h1.strip().lower())

    subtopic_counts: dict[str, int] = {}
    subtopic_display: dict[str, str] = {}
    for f in ok:
        seen_this_page: set[str] = set()
        for h in f.page.headings:  # type: ignore[union-attr]
            if h.get("level") not in ("h2", "h3"):
                continue
            text = (h.get("text") or "").strip()
            key = text.lower()
            if not key or key in own_headings or key in seen_this_page:
                continue
            seen_this_page.add(key)
            subtopic_counts[key] = subtopic_counts.get(key, 0) + 1
            subtopic_display.setdefault(key, text)

    missing = sorted(
        (k for k, count in subtopic_counts.items() if count >= 2),
        key=lambda k: -subtopic_counts[k],
    )[:15]

    this_words = this_page.word_count if this_page else None
    return this_words, median_words, round(avg_h2, 1), [subtopic_display[k] for k in missing]


async def analyse_competitors(
    db: Session,
    website: Website,
    *,
    keyword: str,
    page_id: int | None = None,
) -> CompetitorAnalysisOutcome:
    """Run one full competitor analysis: SERP lookup, fetch the top-N ranking pages, persist,
    and summarise the content gap against ``page_id`` when one is given."""
    keyword = keyword.strip()
    if not keyword:
        return CompetitorAnalysisOutcome(error="A keyword is required.")

    page = db.get(Page, page_id) if page_id else None
    if page_id and page is None:
        return CompetitorAnalysisOutcome(error=f"Page {page_id} not found.")

    try:
        serp = await search(keyword)
    except SerpApiError as exc:
        logger.warning("SerpApi lookup failed for %r: %s", keyword, exc)
        return CompetitorAnalysisOutcome(keyword=keyword, error=str(exc))

    fetches = await fetch_competitors(serp.organic_results)
    this_words, median_words, avg_h2, missing_subtopics = _gap_summary(page, fetches)

    analysis = CompetitorAnalysis(
        website_id=website.id,
        page_id=page.id if page else None,
        keyword=keyword,
        search_location=None,
        search_language=None,
        paa_questions=serp.paa_questions,
        related_searches=serp.related_searches,
        this_page_word_count=this_words,
        competitor_median_word_count=median_words,
        competitor_avg_h2_count=avg_h2,
        missing_subtopics=missing_subtopics,
        fetched_count=sum(1 for f in fetches if f.fetch_status == "ok"),
        failed_count=sum(1 for f in fetches if f.fetch_status != "ok"),
        analysed_at=_now(),
    )
    db.add(analysis)
    db.flush()

    for f in fetches:
        db.add(CompetitorResult(
            competitor_analysis_id=analysis.id,
            position=f.position,
            url=f.url,
            domain=f.domain,
            title=f.title,
            snippet=f.snippet,
            fetch_status=f.fetch_status,
            fetch_error=f.fetch_error,
            word_count=f.page.word_count if f.page else None,
            h1_text=f.page.h1 if f.page else None,
            h1_count=f.page.h1_count if f.page else None,
            h2_count=f.page.h2_count if f.page else None,
            h3_count=f.page.h3_count if f.page else None,
            headings=(f.page.headings[:100] if f.page else []),
        ))

    db.commit()
    db.refresh(analysis)

    logger.info(
        "Competitor analysis for website %s, keyword %r: %d fetched, %d failed, %d PAA questions.",
        website.id, keyword, analysis.fetched_count, analysis.failed_count,
        len(serp.paa_questions),
    )

    return CompetitorAnalysisOutcome(
        analysis_id=analysis.id,
        keyword=keyword,
        fetched_count=analysis.fetched_count,
        failed_count=analysis.failed_count,
        paa_count=len(serp.paa_questions),
        missing_subtopics=missing_subtopics,
    )


def latest_analysis(
    db: Session, website: Website, *, page_id: int | None = None, keyword: str | None = None,
) -> CompetitorAnalysis | None:
    stmt = select(CompetitorAnalysis).where(CompetitorAnalysis.website_id == website.id)
    if page_id is not None:
        stmt = stmt.where(CompetitorAnalysis.page_id == page_id)
    if keyword is not None:
        stmt = stmt.where(CompetitorAnalysis.keyword == keyword)
    return db.scalar(stmt.order_by(CompetitorAnalysis.id.desc()).limit(1))
