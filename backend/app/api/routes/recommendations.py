"""AI recommendation endpoints."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ...config import settings
from ...core.deps import CurrentUser, DbSession, ReadableWebsite, WritableWebsite
from ...core.errors import NotFoundError
from ...core.ratelimit import default_rate_limit
from ...db import SessionLocal
from ...models import AIRecommendation, Page, Website
from ...schemas.common import Page as PageEnvelope
from ...services.ai import analyse_website, available_providers, select_pages

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["recommendations"])


class AnalyseRequest(BaseModel):
    max_pages: int | None = Field(default=None, ge=1, le=10000)
    score_threshold: float | None = Field(default=None, ge=0, le=100)
    page_ids: list[int] | None = Field(default=None, max_length=500)
    force: bool = Field(
        default=False, description="Bypass the selection gate and the unchanged-content cache."
    )
    wait: bool = Field(default=False, description="Run synchronously and return the result.")


class AnalyseResponse(BaseModel):
    website_id: int
    status: str
    provider: str | None = None
    model: str | None = None
    considered: int = 0
    analysed: int = 0
    cached: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = []


class RecommendationSummary(BaseModel):
    id: int
    page_id: int
    url: str
    provider: str
    model: str
    status: str
    summary: str | None
    search_intent: str | None
    priority: str | None
    confidence: float | None
    expected_impact: str | None
    suggested_title: str | None
    suggested_meta_description: str | None
    finding_count: int = 0
    seo_score_at_analysis: float | None = None
    priority_score_at_analysis: float | None = None
    analysed_at: Any = None


def _run_analysis(website_id: int, payload: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is not None:
            asyncio.run(analyse_website(db, website, **payload))
    except Exception as exc:
        logger.exception("AI analysis failed for website %s: %s", website_id, exc)
    finally:
        db.close()


def dispatch_analysis(
    website_id: int, payload: dict[str, Any], background_tasks: BackgroundTasks | None
) -> str:
    if settings.use_celery:
        try:
            from ...services.jobs.tasks import run_ai_task

            run_ai_task.delay(website_id, payload)
            return "celery"
        except Exception as exc:
            logger.warning("Celery dispatch failed for AI analysis: %s", exc)

    if background_tasks is not None:
        background_tasks.add_task(_run_analysis, website_id, payload)
        return "background_task"

    _run_analysis(website_id, payload)
    return "inline"


@router.get("/ai/providers")
def list_providers(_: CurrentUser):
    """Which model backends are configured, and which one is active."""
    return {
        "enabled": settings.ai_enabled,
        "active": settings.llm_provider,
        "configured": available_providers(),
        "max_pages_per_run": settings.ai_max_pages,
        "seo_score_threshold": settings.ai_seo_score_threshold,
    }


@router.get("/websites/{website_id}/ai/selection")
def preview_selection(
    website: ReadableWebsite,
    db: DbSession,
    max_pages: int | None = Query(None, ge=1, le=10000),
    score_threshold: float | None = Query(None, ge=0, le=100),
    limit: int = Query(500, ge=1, le=10000),
):
    """Which pages would be sent to the model, and why — the cost-control view.

    Exposing the gate's reasoning matters: an operator should be able to see that healthy pages are
    being skipped before paying for a run, not after.
    """
    selected, decisions = select_pages(
        db, website, max_pages=max_pages, score_threshold=score_threshold
    )
    return {
        "selected_count": len(selected),
        "considered_count": len(decisions),
        "decisions": [
            {
                "page_id": d.page_id,
                "url": d.url,
                "rank": d.rank,
                "selected": d.selected,
                "reason": d.reason,
            }
            for d in decisions[:limit]
        ],
    }


@router.post(
    "/websites/{website_id}/ai/analyse",
    response_model=AnalyseResponse,
    dependencies=[Depends(default_rate_limit)],
)
def analyse(
    payload: AnalyseRequest,
    website: WritableWebsite,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Run the AI stage over the pages that qualify."""
    options = {
        "max_pages": payload.max_pages,
        "score_threshold": payload.score_threshold,
        "page_ids": payload.page_ids,
        "force": payload.force,
    }

    if not payload.wait:
        transport = dispatch_analysis(website.id, options, background_tasks)
        return AnalyseResponse(
            website_id=website.id, status=f"queued ({transport})"
        )

    outcome = asyncio.run(analyse_website(db, website, **options))
    return AnalyseResponse(
        website_id=website.id,
        status="completed" if not outcome.errors else "completed_with_errors",
        provider=outcome.provider,
        model=outcome.model,
        considered=outcome.considered,
        analysed=outcome.analysed,
        cached=outcome.cached,
        skipped=outcome.skipped,
        failed=outcome.failed,
        errors=outcome.errors[:10],
    )


@router.get(
    "/websites/{website_id}/recommendations",
    response_model=PageEnvelope[RecommendationSummary],
)
def list_recommendations(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    priority: str | None = None,
    status: str = Query("completed"),
):
    """Recommendations across a website, highest business priority first."""
    stmt = (
        select(AIRecommendation, Page.url, Page.priority_score)
        .join(Page, AIRecommendation.page_id == Page.id)
        .where(AIRecommendation.website_id == website.id, AIRecommendation.status == status)
    )
    if priority:
        stmt = stmt.where(AIRecommendation.priority == priority.lower())

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Page.priority_score.desc().nullslast(), AIRecommendation.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return PageEnvelope[RecommendationSummary](
        total=total,
        limit=limit,
        offset=offset,
        items=[
            RecommendationSummary(
                id=rec.id,
                page_id=rec.page_id,
                url=url,
                provider=rec.provider,
                model=rec.model,
                status=rec.status,
                summary=rec.summary,
                search_intent=rec.search_intent,
                priority=rec.priority,
                confidence=rec.confidence,
                expected_impact=rec.expected_impact,
                suggested_title=rec.suggested_title,
                suggested_meta_description=rec.suggested_meta_description,
                finding_count=len((rec.payload or {}).get("findings", [])),
                seo_score_at_analysis=rec.seo_score_at_analysis,
                priority_score_at_analysis=rec.priority_score_at_analysis,
                analysed_at=rec.analysed_at,
            )
            for rec, url, _ in rows
        ],
    )


@router.get("/recommendations/{recommendation_id}")
def get_recommendation(recommendation_id: int, user: CurrentUser, db: DbSession):
    """The full structured recommendation, including every finding and suggested change."""
    row = db.get(AIRecommendation, recommendation_id)
    if row is None:
        raise NotFoundError(f"Recommendation {recommendation_id} was not found.")

    from ...core.deps import get_website_for_read

    get_website_for_read(row.website_id, user, db)
    page = db.get(Page, row.page_id)

    return {
        "id": row.id,
        "page_id": row.page_id,
        "url": page.url if page else None,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "error": row.error,
        "seo_score_at_analysis": row.seo_score_at_analysis,
        "priority_score_at_analysis": row.priority_score_at_analysis,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "latency_ms": row.latency_ms,
        "analysed_at": row.analysed_at,
        "recommendation": row.payload,
    }
