"""Priority scoring and weight configuration endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field

from ...config import settings
from ...core.deps import AdminUser, DbSession, ReadableWebsite, WritableWebsite
from ...core.ratelimit import default_rate_limit
from ...db import SessionLocal
from ...models import SETTING_KEYS, Setting, Website
from ...services.priority import (
    COMPONENTS,
    available_data_sources,
    compute_priorities,
    connected_providers,
    resolve_weights,
    score_website,
    set_weights,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["priority"])


class WeightUpdate(BaseModel):
    seo_severity: float | None = Field(default=None, ge=0, le=1)
    ga4_activity: float | None = Field(default=None, ge=0, le=1)
    gsc_search: float | None = Field(default=None, ge=0, le=1)
    semrush_opportunity: float | None = Field(default=None, ge=0, le=1)


class WeightResponse(BaseModel):
    scope: str
    weights: dict[str, float]
    #: After dropping components whose data source is absent for this website.
    effective_weights: dict[str, float] | None = None
    data_sources: list[str] = []
    connected_providers: list[str] = []


class ScoringResponse(BaseModel):
    website_id: int
    pages_scored: int
    weights: dict[str, float]
    data_sources: list[str]
    window_days: int
    top_pages: list[dict] = []


def _run_scoring(website_id: int, window_days: int | None) -> None:
    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is not None:
            score_website(db, website, window_days=window_days)
    except Exception as exc:
        logger.exception("Priority scoring failed for website %s: %s", website_id, exc)
    finally:
        db.close()


def dispatch_scoring(
    website_id: int, window_days: int | None, background_tasks: BackgroundTasks | None
) -> str:
    if settings.use_celery:
        try:
            from ...services.jobs.tasks import run_scoring_task

            run_scoring_task.delay(website_id, window_days)
            return "celery"
        except Exception as exc:
            logger.warning("Celery dispatch failed for priority scoring: %s", exc)

    if background_tasks is not None:
        background_tasks.add_task(_run_scoring, website_id, window_days)
        return "background_task"

    _run_scoring(website_id, window_days)
    return "inline"


@router.post(
    "/websites/{website_id}/priority/score",
    response_model=ScoringResponse,
    dependencies=[Depends(default_rate_limit)],
)
def rescore(
    website: WritableWebsite,
    db: DbSession,
    window_days: int | None = Query(None, ge=1, le=365),
    wait: bool = Query(
        True, description="Score synchronously and return the result (small sites)."
    ),
    background_tasks: BackgroundTasks = None,
):
    """Recompute priority scores for every page on a website."""
    if not wait:
        transport = dispatch_scoring(website.id, window_days, background_tasks)
        return ScoringResponse(
            website_id=website.id,
            pages_scored=0,
            weights=resolve_weights(db, website.id),
            data_sources=sorted(available_data_sources(db, website.id)),
            window_days=window_days or settings.priority_metric_window_days,
            top_pages=[{"transport": transport, "status": "queued"}],
        )

    result = score_website(db, website, window_days=window_days)
    return ScoringResponse(
        website_id=website.id,
        pages_scored=result.pages_scored,
        weights=result.weights,
        data_sources=result.data_sources,
        window_days=result.window_days,
        top_pages=[
            {
                "page_id": p.page_id,
                "url": p.url,
                "priority_score": p.score,
                "band": p.band,
                "rank": p.rank,
                "components": p.components,
            }
            for p in result.priorities[:20]
        ],
    )


@router.get("/websites/{website_id}/priority/preview", response_model=ScoringResponse)
def preview(
    website: ReadableWebsite,
    db: DbSession,
    window_days: int | None = Query(None, ge=1, le=365),
    seo_severity: float | None = Query(None, ge=0, le=1),
    ga4_activity: float | None = Query(None, ge=0, le=1),
    gsc_search: float | None = Query(None, ge=0, le=1),
    semrush_opportunity: float | None = Query(None, ge=0, le=1),
    limit: int = Query(20, ge=1, le=200),
):
    """Score with hypothetical weights **without saving** — the settings screen's live preview."""
    from ...services.priority.weights import normalise

    overrides = {
        "seo_severity": seo_severity,
        "ga4_activity": ga4_activity,
        "gsc_search": gsc_search,
        "semrush_opportunity": semrush_opportunity,
    }
    supplied = {k: v for k, v in overrides.items() if v is not None}
    weights = (
        normalise({**resolve_weights(db, website.id), **supplied})
        if supplied
        else resolve_weights(db, website.id)
    )

    result = compute_priorities(db, website, window_days=window_days, weights=weights)
    return ScoringResponse(
        website_id=website.id,
        pages_scored=result.pages_scored,
        weights=result.weights,
        data_sources=result.data_sources,
        window_days=result.window_days,
        top_pages=[
            {
                "page_id": p.page_id,
                "url": p.url,
                "priority_score": p.score,
                "band": p.band,
                "rank": p.rank,
                "components": p.components,
                "seo_score": None,
            }
            for p in result.priorities[:limit]
        ],
    )


@router.get("/websites/{website_id}/priority/weights", response_model=WeightResponse)
def get_website_weights(website: ReadableWebsite, db: DbSession):
    from ...services.priority.weights import redistribute

    configured = resolve_weights(db, website.id)
    sources = available_data_sources(db, website.id)
    return WeightResponse(
        scope=f"website:{website.id}",
        weights=configured,
        effective_weights=redistribute(configured, sources),
        data_sources=sorted(sources),
        connected_providers=sorted(connected_providers(db, website.id)),
    )


@router.put("/websites/{website_id}/priority/weights", response_model=WeightResponse)
def update_website_weights(payload: WeightUpdate, website: WritableWebsite, db: DbSession):
    """Override the priority weights for one website. Values are renormalised to sum to 1."""
    from ...services.priority.weights import redistribute

    saved = set_weights(
        db, payload.model_dump(exclude_none=True), website_id=website.id
    )
    sources = available_data_sources(db, website.id)
    return WeightResponse(
        scope=f"website:{website.id}",
        weights=saved,
        effective_weights=redistribute(saved, sources),
        data_sources=sorted(sources),
        connected_providers=sorted(connected_providers(db, website.id)),
    )


@router.get("/settings/priority/weights", response_model=WeightResponse)
def get_global_weights(db: DbSession, _: AdminUser):
    return WeightResponse(scope="global", weights=resolve_weights(db, None))


@router.put("/settings/priority/weights", response_model=WeightResponse)
def update_global_weights(payload: WeightUpdate, db: DbSession, _: AdminUser):
    """Set the platform-wide defaults. Per-website overrides continue to take precedence."""
    return WeightResponse(
        scope="global", weights=set_weights(db, payload.model_dump(exclude_none=True))
    )


@router.get("/settings")
def list_settings(db: DbSession, _: AdminUser):
    """Every configurable setting and its current value."""
    rows = db.query(Setting).all()
    return {
        "known_keys": SETTING_KEYS,
        "priority_components": list(COMPONENTS),
        "defaults": {
            "priority_weights": settings.default_priority_weights,
            "seo_weights": settings.seo_weights,
            "ai_max_pages": settings.ai_max_pages,
            "ai_seo_score_threshold": settings.ai_seo_score_threshold,
            "priority_metric_window_days": settings.priority_metric_window_days,
        },
        "overrides": [
            {
                "id": row.id,
                "scope": f"website:{row.website_id}" if row.website_id else "global",
                "key": row.key,
                "value": row.value,
            }
            for row in rows
        ],
    }
