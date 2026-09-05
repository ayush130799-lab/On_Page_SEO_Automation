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
from ...models.intent import KeywordOpportunity, PageIntentProfile
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
    search_impact_score: float | None = None
    user_activity_score: float | None = None
    impact_score: float | None = None
    reason: str | None = None
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
                search_impact_score=rec.search_impact_score,
                user_activity_score=rec.user_activity_score,
                impact_score=rec.impact_score,
                reason=rec.reason,
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


# ───────────────────────────────────────────────────────────────────────────────
# Phase 2: Search Intent & Keyword Intelligence endpoints
# ───────────────────────────────────────────────────────────────────────────────


class IntentAnalyseRequest(BaseModel):
    page_ids: list[int] | None = Field(default=None, max_length=500)
    force: bool = Field(
        default=False,
        description="Re-analyse pages that already have an intent profile.",
    )
    wait: bool = Field(default=False, description="Run synchronously and return the result.")


class KeywordOpportunityOut(BaseModel):
    keyword: str
    tier: str
    # §5.4's full factor set — Demand x RankingOpp x IntentMatch x BusinessRel x ContentRel x
    # CompetitionOpp — so a ranked table can show *why* one keyword outranks another, not just
    # the composite.
    demand_score: float | None
    ranking_opportunity_score: float | None
    intent_match_score: float | None
    business_relevance_score: float | None
    content_relevance_score: float | None
    competition_opportunity_score: float | None
    keyword_opportunity_score: float | None
    current_position: float | None
    current_impressions: int | None
    source: str | None


class IntentProfileOut(BaseModel):
    page_id: int
    url: str
    detected_intent: str | None
    intent_confidence: float | None
    detection_method: str | None
    business_intent: str | None
    #: §6.1's second axis: commercial | informational | hybrid.
    page_type: str | None
    intent_mismatch: bool
    mismatch_severity: str | None
    mismatch_explanation: str | None
    #: gsc_queries | page_targeting | none — which evidence produced the mismatch verdict.
    mismatch_evidence: str | None
    primary_keywords: list[str] | None
    secondary_keywords: list[str] | None
    long_tail_keywords: list[str] | None
    semantic_entities: list[str] | None
    question_keywords: list[str] | None
    keyword_opportunity_score: float | None
    keywords: list[KeywordOpportunityOut] = []
    analysed_at: Any = None


class MismatchListItem(BaseModel):
    page_id: int
    url: str
    mismatch_severity: str
    business_intent: str | None
    detected_intent: str | None
    mismatch_explanation: str | None
    keyword_opportunity_score: float | None
    analysed_at: Any = None


def _run_intent_analysis(website_id: int, payload: dict[str, Any]) -> None:
    from ...db import SessionLocal
    from ...services.intent import analyse_intent_for_website

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is not None:
            analyse_intent_for_website(db, website, **payload)
    except Exception as exc:
        logger.exception("Intent analysis failed for website %s: %s", website_id, exc)
    finally:
        db.close()


@router.post(
    "/websites/{website_id}/intent/analyse",
    dependencies=[Depends(default_rate_limit)],
    summary="Trigger Phase 2 intent analysis for a website",
    tags=["intent"],
)
def analyse_intent(
    payload: IntentAnalyseRequest,
    website: WritableWebsite,
    background_tasks: BackgroundTasks,
):
    """Run the Phase 2 intent classification, mismatch detection and keyword generation pipeline.

    By default, only pages that do not yet have an intent profile are processed.
    Pass ``force=true`` to re-analyse all pages.
    """
    options = {"page_ids": payload.page_ids, "force": payload.force}

    if not payload.wait:
        background_tasks.add_task(_run_intent_analysis, website.id, options)
        return {"website_id": website.id, "status": "queued"}

    from ...db import SessionLocal
    from ...services.intent import analyse_intent_for_website

    db_sync = SessionLocal()
    try:
        outcome = analyse_intent_for_website(db_sync, website, **options)
        return {
            "website_id": website.id,
            "status": "completed",
            "considered": outcome.considered,
            "classified": outcome.classified,
            "mismatches_found": outcome.mismatches_found,
            "failed": outcome.failed,
            "errors": outcome.errors[:10],
        }
    finally:
        db_sync.close()


@router.get(
    "/websites/{website_id}/pages/{page_id}/intent",
    response_model=IntentProfileOut,
    summary="Get a page's search intent profile and keyword tiers",
    tags=["intent"],
)
def get_page_intent(
    page_id: int,
    website: ReadableWebsite,
    db: DbSession,
):
    """Returns the latest intent classification, mismatch analysis, and keyword opportunity matrix
    for one page.
    """
    profile = db.scalar(
        select(PageIntentProfile).where(
            PageIntentProfile.page_id == page_id,
            PageIntentProfile.website_id == website.id,
        )
    )
    if profile is None:
        raise NotFoundError(
            f"No intent profile found for page {page_id}. Run /intent/analyse first."
        )

    page = db.get(Page, page_id)
    keywords = db.scalars(
        select(KeywordOpportunity)
        .where(KeywordOpportunity.intent_profile_id == profile.id)
        .order_by(KeywordOpportunity.keyword_opportunity_score.desc().nullslast())
    ).all()

    return IntentProfileOut(
        page_id=page_id,
        url=page.url if page else "",
        detected_intent=profile.detected_intent,
        intent_confidence=profile.intent_confidence,
        detection_method=profile.detection_method,
        business_intent=profile.business_intent,
        page_type=profile.page_type,
        intent_mismatch=profile.intent_mismatch,
        mismatch_severity=profile.mismatch_severity,
        mismatch_explanation=profile.mismatch_explanation,
        mismatch_evidence=profile.mismatch_evidence,
        primary_keywords=profile.primary_keywords,
        secondary_keywords=profile.secondary_keywords,
        long_tail_keywords=profile.long_tail_keywords,
        semantic_entities=profile.semantic_entities,
        question_keywords=profile.question_keywords,
        keyword_opportunity_score=profile.keyword_opportunity_score,
        analysed_at=profile.analysed_at,
        keywords=[
            KeywordOpportunityOut(
                keyword=kw.keyword,
                tier=kw.keyword_tier,
                demand_score=kw.demand_score,
                ranking_opportunity_score=kw.ranking_opportunity_score,
                intent_match_score=kw.intent_match_score,
                business_relevance_score=kw.business_relevance_score,
                content_relevance_score=kw.content_relevance_score,
                competition_opportunity_score=kw.competition_opportunity_score,
                keyword_opportunity_score=kw.keyword_opportunity_score,
                current_position=kw.current_position,
                current_impressions=kw.current_impressions,
                source=kw.source,
            )
            for kw in keywords
        ],
    )


@router.get(
    "/websites/{website_id}/keywords",
    summary="Site-wide ranked keyword opportunity table (roadmap §5.4)",
    tags=["intent"],
)
def list_keyword_opportunities(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    tier: str | None = Query(None, description="primary | secondary | long_tail | semantic | question"),
    q: str | None = Query(None, description="Substring filter on the keyword text"),
):
    """The keyword catalog across the whole site, ranked by opportunity.

    Roadmap §11 lists ``keywords`` as a table distinct from ``keyword_opportunities``. The two
    would hold the same fields for the same rows — a keyword's opportunity only exists in the
    context of a specific page's intent and content, which is exactly what
    ``keyword_opportunities`` already stores. Duplicating it into a second table would mean
    keeping both in sync on every re-score for no additional information. This endpoint is that
    catalog: the same per-page rows, grouped by keyword text so the same term appearing on
    several pages (each independently scored against its own page) is visible as one entry with
    its best-scoring page surfaced and every page that targets it listed.
    """
    from ...models.intent import KeywordOpportunity

    stmt = select(KeywordOpportunity, Page.url, Page.path).join(
        Page, KeywordOpportunity.page_id == Page.id
    ).where(KeywordOpportunity.website_id == website.id)
    if tier:
        stmt = stmt.where(KeywordOpportunity.keyword_tier == tier)
    if q:
        stmt = stmt.where(KeywordOpportunity.keyword.ilike(f"%{q}%"))

    rows = db.execute(stmt).all()

    grouped: dict[str, dict[str, Any]] = {}
    for kw, url, path in rows:
        entry = grouped.setdefault(kw.keyword, {
            "keyword": kw.keyword,
            "tier": kw.keyword_tier,
            "best_score": -1.0,
            "pages": [],
        })
        entry["pages"].append({
            "page_id": kw.page_id,
            "url": url,
            "path": path,
            "keyword_opportunity_score": kw.keyword_opportunity_score,
            "current_position": kw.current_position,
            "current_impressions": kw.current_impressions,
            "source": kw.source,
        })
        score = kw.keyword_opportunity_score or 0.0
        if score > entry["best_score"]:
            entry["best_score"] = score
            entry.update({
                "demand_score": kw.demand_score,
                "ranking_opportunity_score": kw.ranking_opportunity_score,
                "intent_match_score": kw.intent_match_score,
                "business_relevance_score": kw.business_relevance_score,
                "content_relevance_score": kw.content_relevance_score,
                "competition_opportunity_score": kw.competition_opportunity_score,
                "best_page_id": kw.page_id,
                "best_page_url": url,
            })

    ranked = sorted(grouped.values(), key=lambda e: e["best_score"], reverse=True)
    for entry in ranked:
        entry["page_count"] = len(entry["pages"])
        entry["keyword_opportunity_score"] = entry.pop("best_score")

    total = len(ranked)
    page_slice = ranked[offset:offset + limit]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": page_slice,
    }


@router.get(
    "/websites/{website_id}/intent/mismatches",
    summary="List pages with intent mismatches, sorted by severity",
    tags=["intent"],
)
def list_intent_mismatches(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    severity: str | None = Query(None, description="P0 | P1 | P2 | P3"),
):
    """Returns all pages on the website where the business intent conflicts with the
    ranking query intent.  Results are ordered P0 → P3 (most critical first), then
    by keyword opportunity score descending.
    """
    stmt = (
        select(PageIntentProfile, Page.url)
        .join(Page, PageIntentProfile.page_id == Page.id)
        .where(
            PageIntentProfile.website_id == website.id,
            PageIntentProfile.intent_mismatch.is_(True),
        )
    )
    if severity:
        stmt = stmt.where(PageIntentProfile.mismatch_severity == severity.upper())

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    # Order: P0 first (severity sort), then by keyword opportunity score
    _severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    rows = db.execute(stmt.limit(limit).offset(offset)).all()
    rows.sort(
        key=lambda r: (
            _severity_order.get(r[0].mismatch_severity or "P3", 3),
            -(r[0].keyword_opportunity_score or 0),
        )
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            MismatchListItem(
                page_id=profile.page_id,
                url=url,
                mismatch_severity=profile.mismatch_severity or "P3",
                business_intent=profile.business_intent,
                detected_intent=profile.detected_intent,
                mismatch_explanation=profile.mismatch_explanation,
                keyword_opportunity_score=profile.keyword_opportunity_score,
                analysed_at=profile.analysed_at,
            )
            for profile, url in rows
        ],
    }


@router.get(
    "/websites/{website_id}/opportunities",
    summary="Ranked recommendations across a website (roadmap §10.2)",
    tags=["opportunities"],
)
def list_opportunities(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    priority_level: str | None = Query(None, description="P0 | P1 | P2 | P3"),
    recommendation_type: str | None = Query(None),
    status: str | None = Query(None, description="detected | approved | … | validated"),
):
    """Every scored recommendation on the website, most impactful first.

    This is the data behind §10.2's Top Opportunities list. Each row carries both §4.4 scores
    separately, the confidence, and — per §9.1 — the reason and expected outcome, so no caller
    can render a bare number without its explanation.
    """
    from ...models import RecommendationScore

    stmt = (
        select(RecommendationScore, Page.url, Page.path)
        .join(Page, RecommendationScore.page_id == Page.id)
        .where(RecommendationScore.website_id == website.id)
    )
    if priority_level:
        stmt = stmt.where(RecommendationScore.priority_level == priority_level.upper())
    if recommendation_type:
        stmt = stmt.where(RecommendationScore.recommendation_type == recommendation_type)
    if status:
        stmt = stmt.where(RecommendationScore.status == status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    rows = db.execute(
        stmt.order_by(RecommendationScore.overall_priority.desc().nullslast())
        .limit(limit)
        .offset(offset)
    ).all()

    counts = dict(
        db.execute(
            select(RecommendationScore.priority_level, func.count())
            .where(RecommendationScore.website_id == website.id)
            .group_by(RecommendationScore.priority_level)
        ).all()
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "priority_counts": {level: counts.get(level, 0) for level in ("P0", "P1", "P2", "P3")},
        "items": [
            {
                "id": score.id,
                "page_id": score.page_id,
                "url": url,
                "path": path,
                "recommendation_type": score.recommendation_type,
                "title": score.title,
                "current_state": score.current_state,
                "recommended_state": score.recommended_state,
                "search_intent": score.search_intent,
                "primary_keyword": score.primary_keyword,
                # §4.4 — reported separately, never collapsed into one figure.
                "search_impact_score": score.search_impact_score,
                "user_activity_score": score.user_activity_score,
                "business_impact_score": score.business_impact_score,
                "overall_priority": score.overall_priority,
                "confidence_score": score.confidence_score,
                "priority_level": score.priority_level,
                "severity": score.severity,
                "effort": score.effort,
                # §9.1 — the explanation travels with the number.
                "reason": score.reason,
                "expected_outcome": score.expected_outcome,
                "tier": score.tier,
                "factors": score.factors,
                "status": score.status,
                "scored_at": score.scored_at.isoformat() if score.scored_at else None,
            }
            for score, url, path in rows
        ],
    }


@router.get(
    "/websites/{website_id}/pages/{page_id}/opportunities",
    summary="Ranked recommendations for one URL (roadmap §7.4 / §10.3)",
    tags=["opportunities"],
)
def page_opportunities(
    page_id: int,
    website: ReadableWebsite,
    db: DbSession,
):
    """The §7.4 URL-level action plan: current performance, intent, target keywords, detected
    problems, recommended actions, and — when a live SerpApi competitor analysis has been run
    for this page — the competitor/SERP comparison. That analysis is on-demand (§4.2/§7.4), not
    computed automatically here, so this reports the most recent run rather than always fresh
    data; when none has been run yet, that is stated rather than invented.
    """
    from ...models import CompetitorAnalysis, RecommendationScore
    from ...services.metrics import aggregate_page_metrics

    page = db.scalar(
        select(Page).where(Page.id == page_id, Page.website_id == website.id)
    )
    if page is None:
        raise NotFoundError(f"Page {page_id} not found on website {website.id}.")

    profile = db.scalar(
        select(PageIntentProfile).where(PageIntentProfile.page_id == page.id)
    )
    scores = db.scalars(
        select(RecommendationScore)
        .where(RecommendationScore.page_id == page.id)
        .order_by(RecommendationScore.overall_priority.desc().nullslast())
    ).all()
    metrics = aggregate_page_metrics(db, [page.id]).get(page.id, {})
    competitor_analysis = db.scalar(
        select(CompetitorAnalysis)
        .where(CompetitorAnalysis.page_id == page.id)
        .order_by(CompetitorAnalysis.id.desc())
        .limit(1)
    )

    return {
        "page_id": page.id,
        "url": page.url,
        # -> Current SEO Score
        "seo_score": page.seo_score,
        # -> Current Search Performance (§4.2's GSC signals)
        "current_search_performance": {
            "impressions": metrics.get("impressions"),
            "clicks": metrics.get("clicks"),
            "ctr": metrics.get("ctr"),
            "position": metrics.get("position"),
        },
        # -> Current User Activity (§4.2's GA4 signals)
        "current_user_activity": {
            "sessions": metrics.get("sessions"),
            "engagement_rate": metrics.get("engagement_rate"),
            "conversions": metrics.get("conversions"),
            "revenue": metrics.get("revenue"),
        },
        # -> Search Intent
        "search_intent": profile.detected_intent if profile else None,
        "page_type": profile.page_type if profile else None,
        "intent_mismatch": bool(profile and profile.intent_mismatch),
        "mismatch_explanation": profile.mismatch_explanation if profile else None,
        # -> Target Keywords
        "target_keywords": {
            "primary": (profile.primary_keywords if profile else None) or [],
            "secondary": (profile.secondary_keywords if profile else None) or [],
            "long_tail": (profile.long_tail_keywords if profile else None) or [],
            "question": (profile.question_keywords if profile else None) or [],
        },
        "keyword_opportunity_score": profile.keyword_opportunity_score if profile else None,
        # -> Competitor/SERP Analysis (§4.2/§7.4) — the most recent on-demand SerpApi run for
        # this page, when one exists. Never fabricated: absence is reported plainly.
        "competitor_analysis": (
            {
                "available": False,
                "reason": (
                    "No competitor analysis has been run for this page yet. "
                    "POST /pages/{page_id}/competitors/analyse to run one."
                ),
            }
            if competitor_analysis is None
            else {
                "available": True,
                "keyword": competitor_analysis.keyword,
                "analysed_at": (
                    competitor_analysis.analysed_at.isoformat()
                    if competitor_analysis.analysed_at else None
                ),
                "this_page_word_count": competitor_analysis.this_page_word_count,
                "competitor_median_word_count": competitor_analysis.competitor_median_word_count,
                "competitor_avg_h2_count": competitor_analysis.competitor_avg_h2_count,
                "missing_subtopics": competitor_analysis.missing_subtopics,
                "paa_questions": competitor_analysis.paa_questions,
                "fetched_count": competitor_analysis.fetched_count,
            }
        ),
        # -> Detected Problems / Potential Opportunities / Impact Score / Recommended Actions /
        #    Expected Outcome
        "recommended_actions": [
            {
                "recommendation_type": s.recommendation_type,
                "title": s.title,
                "priority_level": s.priority_level,
                "search_impact_score": s.search_impact_score,
                "user_activity_score": s.user_activity_score,
                "overall_priority": s.overall_priority,
                "confidence_score": s.confidence_score,
                "effort": s.effort,
                "current_state": s.current_state,
                "recommended_state": s.recommended_state,
                "reason": s.reason,
                "expected_outcome": s.expected_outcome,
                "status": s.status,
            }
            for s in scores
        ],
    }
