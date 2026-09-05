"""Live SERP competitor analysis — roadmap §4.2 / §7.4.

On-demand only, mirroring the recommendations.py ``wait``/background-dispatch convention: a
caller either waits for the full SerpApi + competitor-fetch round trip synchronously, or gets a
202 immediately while it runs in the background. Never triggered automatically by a crawl —
every call costs a SerpApi credit plus bandwidth fetching competitor pages, and running it
unconditionally for every page's every keyword would repeat the exact cost mistake Step 1's
tiered AI routing exists to avoid.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...config import settings
from ...core.deps import DbSession, ReadableWebsite, WritableWebsite
from ...core.errors import NotFoundError
from ...core.ratelimit import default_rate_limit
from ...db import SessionLocal
from ...models import CompetitorAnalysis, CompetitorResult, Page, PageIntentProfile
from ...services.serp import analyse_competitors, is_configured, latest_analysis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["competitors"])


class CompetitorAnalyseRequest(BaseModel):
    #: Optional — falls back to the page's own primary keyword (from Step 2's intent profile)
    #: when the page has one, so a caller who already has a keyword picked doesn't need to repeat it.
    keyword: str | None = Field(default=None, max_length=255)
    wait: bool = Field(default=False, description="Run synchronously and return the result.")


def _execute_competitor_analysis(website_id: int, keyword: str, page_id: int | None) -> None:
    """Run analysis in its own session (background task entry point)."""
    from ...models import Website

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return
        asyncio.run(analyse_competitors(db, website, keyword=keyword, page_id=page_id))
    except Exception:
        logger.exception("Competitor analysis did not complete for website %s.", website_id)
    finally:
        db.close()


def dispatch_competitor_analysis(
    website_id: int, keyword: str, page_id: int | None,
    background_tasks: BackgroundTasks | None,
) -> str:
    """Send a competitor analysis to Celery when configured, otherwise a background task —
    the same dispatch shape as ``crawls.dispatch_crawl`` and ``webhooks.dispatch_pr_analysis``."""
    if settings.use_celery:
        try:
            from ...services.jobs.tasks import run_competitor_analysis_task

            run_competitor_analysis_task.delay(website_id, keyword, page_id)
            return "celery"
        except Exception as exc:
            logger.warning(
                "Celery dispatch failed for competitor analysis on website %s; falling back: %s",
                website_id, exc,
            )

    if background_tasks is not None:
        background_tasks.add_task(_execute_competitor_analysis, website_id, keyword, page_id)
        return "background_task"

    _execute_competitor_analysis(website_id, keyword, page_id)
    return "inline"


def _serialise(analysis: CompetitorAnalysis, results: list[CompetitorResult]) -> dict:
    return {
        "id": analysis.id,
        "keyword": analysis.keyword,
        "page_id": analysis.page_id,
        "analysed_at": analysis.analysed_at.isoformat() if analysis.analysed_at else None,
        "fetched_count": analysis.fetched_count,
        "failed_count": analysis.failed_count,
        "paa_questions": [
            q if isinstance(q, str) else (q.get("question") if isinstance(q, dict) else str(q))
            for q in (analysis.paa_questions or [])
            if (q if isinstance(q, str) else (q.get("question") if isinstance(q, dict) else str(q)))
        ] if analysis.paa_questions else [],
        "related_searches": [
            q if isinstance(q, str) else (q.get("query") if isinstance(q, dict) else str(q))
            for q in (analysis.related_searches or [])
            if (q if isinstance(q, str) else (q.get("query") if isinstance(q, dict) else str(q)))
        ] if analysis.related_searches else [],
        "content_gap": {
            "this_page_word_count": analysis.this_page_word_count,
            "competitor_median_word_count": analysis.competitor_median_word_count,
            "competitor_avg_h2_count": analysis.competitor_avg_h2_count,
            "missing_subtopics": analysis.missing_subtopics,
        },
        "competitors": [
            {
                "position": r.position,
                "url": r.url,
                "domain": r.domain,
                "title": r.title,
                "snippet": r.snippet,
                "fetch_status": r.fetch_status,
                "fetch_error": r.fetch_error,
                "word_count": r.word_count,
                "h1_text": r.h1_text,
                "h1_count": r.h1_count,
                "h2_count": r.h2_count,
                "h3_count": r.h3_count,
                "headings": r.headings,
            }
            for r in results
        ],
    }


@router.get("/serp/status")
def serp_status():
    """Whether SerpApi is configured — lets the frontend show a setup prompt instead of a
    confusing failure the first time someone tries this feature."""
    return {"configured": is_configured()}


@router.post(
    "/websites/{website_id}/pages/{page_id}/competitors/analyse",
    dependencies=[Depends(default_rate_limit)],
)
def analyse_page_competitors(
    payload: CompetitorAnalyseRequest,
    page_id: int,
    website: WritableWebsite,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Analyse the competitors currently ranking for this page's target keyword."""
    page = db.get(Page, page_id)
    if page is None or page.website_id != website.id:
        raise NotFoundError(f"Page {page_id} not found on website {website.id}.")

    keyword = payload.keyword
    if not keyword:
        profile = db.scalar(
            select(PageIntentProfile).where(PageIntentProfile.page_id == page.id)
        )
        if profile and profile.primary_keywords:
            keyword = profile.primary_keywords[0]

    if not keyword:
        return {
            "status": "error",
            "reason": (
                "No keyword was given, and this page has no primary keyword yet (run "
                "/intent/analyse first, or pass a keyword explicitly)."
            ),
        }

    if not is_configured():
        return {
            "status": "error",
            "reason": "SERPAPI_KEY is not configured. Competitor analysis is inactive.",
        }

    if not payload.wait:
        transport = dispatch_competitor_analysis(website.id, keyword, page.id, background_tasks)
        return {"status": f"queued ({transport})", "keyword": keyword}

    outcome = asyncio.run(analyse_competitors(db, website, keyword=keyword, page_id=page.id))
    if outcome.error:
        return {"status": "error", "keyword": keyword, "reason": outcome.error}
    return {
        "status": "completed",
        "keyword": keyword,
        "analysis_id": outcome.analysis_id,
        "fetched_count": outcome.fetched_count,
        "failed_count": outcome.failed_count,
        "paa_count": outcome.paa_count,
        "missing_subtopics": outcome.missing_subtopics,
    }


@router.get("/websites/{website_id}/pages/{page_id}/competitors")
def get_page_competitors(
    page_id: int,
    website: ReadableWebsite,
    db: DbSession,
):
    """The most recent competitor analysis for this page, if one has been run."""
    page = db.get(Page, page_id)
    if page is None or page.website_id != website.id:
        raise NotFoundError(f"Page {page_id} not found on website {website.id}.")

    analysis = latest_analysis(db, website, page_id=page.id)
    if analysis is None:
        return {
            "available": False,
            "reason": "No competitor analysis has been run for this page yet.",
        }
    return {"available": True, **_serialise(analysis, list(analysis.results))}
