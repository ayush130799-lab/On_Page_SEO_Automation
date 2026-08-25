"""Crawl-run endpoints: trigger a crawl, watch its progress, review its history."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import func, select

from ...config import settings
from ...core.deps import CurrentUser, DbSession, ReadableWebsite, WritableWebsite
from ...core.errors import ConflictError, NotFoundError
from ...core.ratelimit import default_rate_limit
from ...db import SessionLocal
from ...models import CrawlMode, CrawlRun, CrawlTrigger, RunStatus, Website
from ...schemas.common import Page as PageEnvelope
from ...schemas.crawl import CrawlCreate, CrawlRunResponse, RuleInfo
from ...services.pipeline import create_crawl_run, resolve_incremental_urls, run_crawl_pipeline
from ...services.seo import rule_catalogue

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["crawls"])

ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


def _execute_crawl(crawl_run_id: int) -> None:
    """Run the pipeline in its own session (background task / worker entry point)."""
    db = SessionLocal()
    try:
        asyncio.run(run_crawl_pipeline(db, crawl_run_id))
    except Exception as exc:
        logger.exception("Crawl run %s did not complete: %s", crawl_run_id, exc)
    finally:
        db.close()


def dispatch_crawl(crawl_run_id: int, background_tasks: BackgroundTasks | None = None) -> str:
    """Send a crawl to Celery when configured, otherwise run it as a background task.

    Returns the transport actually used, which the API echoes back so an operator can tell whether
    the worker fleet picked the job up.
    """
    if settings.use_celery:
        try:
            from ...services.jobs.tasks import run_crawl_task

            run_crawl_task.delay(crawl_run_id)
            return "celery"
        except Exception as exc:
            logger.warning(
                "Celery dispatch failed for crawl run %s; falling back to an in-process task: %s",
                crawl_run_id,
                exc,
            )

    if background_tasks is not None:
        background_tasks.add_task(_execute_crawl, crawl_run_id)
        return "background_task"

    _execute_crawl(crawl_run_id)
    return "inline"


@router.post(
    "/websites/{website_id}/crawls",
    response_model=CrawlRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(default_rate_limit)],
)
def start_crawl(
    payload: CrawlCreate,
    website: WritableWebsite,
    user: CurrentUser,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Queue a crawl. One active run per website — re-crawling mid-run would double the work."""
    active = db.scalar(
        select(CrawlRun).where(
            CrawlRun.website_id == website.id, CrawlRun.status.in_(ACTIVE_STATUSES)
        )
    )
    if active is not None:
        raise ConflictError(
            f"Crawl run {active.id} is already {active.status} for this website.",
            {"crawl_run_id": active.id, "status": active.status},
        )

    target_urls = None
    if payload.mode == CrawlMode.INCREMENTAL:
        if not payload.target_urls:
            from ...core.errors import ValidationError

            raise ValidationError("Incremental crawls require at least one target URL.")
        target_urls = resolve_incremental_urls(website, payload.target_urls)

    run = create_crawl_run(
        db,
        website,
        trigger=CrawlTrigger.MANUAL,
        mode=payload.mode,
        target_urls=target_urls,
        triggered_by_id=user.id,
        # Scoped to this run; the website's configured limit is left alone.
        max_pages=payload.max_pages,
    )
    transport = dispatch_crawl(run.id, background_tasks)
    logger.info("Crawl run %s dispatched via %s for website %s", run.id, transport, website.id)
    return run


@router.get("/websites/{website_id}/crawls", response_model=PageEnvelope[CrawlRunResponse])
def list_crawls(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    stmt = select(CrawlRun).where(CrawlRun.website_id == website.id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(CrawlRun.id.desc()).limit(limit).offset(offset)
    ).all()
    return PageEnvelope[CrawlRunResponse](
        total=total,
        limit=limit,
        offset=offset,
        items=[CrawlRunResponse.model_validate(row) for row in rows],
    )


@router.get("/crawls/{crawl_run_id}", response_model=CrawlRunResponse)
def get_crawl(crawl_run_id: int, user: CurrentUser, db: DbSession):
    """Poll a crawl run. The dashboard hits this on an interval while a run is active."""
    run = db.get(CrawlRun, crawl_run_id)
    if run is None:
        raise NotFoundError(f"Crawl run {crawl_run_id} was not found.")

    from ...core.deps import get_website_for_read

    get_website_for_read(run.website_id, user, db)  # authorization check
    return run


@router.post("/crawls/{crawl_run_id}/cancel", response_model=CrawlRunResponse)
def cancel_crawl(crawl_run_id: int, user: CurrentUser, db: DbSession):
    """Mark a run cancelled so a stuck job stops blocking new crawls for the website."""
    run = db.get(CrawlRun, crawl_run_id)
    if run is None:
        raise NotFoundError(f"Crawl run {crawl_run_id} was not found.")

    from ...core.deps import get_website_for_write

    get_website_for_write(run.website_id, user, db)

    if run.status not in ACTIVE_STATUSES:
        raise ConflictError(f"Crawl run {crawl_run_id} is already {run.status}.")

    run.status = RunStatus.CANCELLED
    run.stage = "cancelled"
    db.commit()
    db.refresh(run)
    return run


@router.get("/seo/rules", response_model=list[RuleInfo], tags=["seo"])
def list_rules(_: CurrentUser):
    """Every rule the engine will apply, with its weight and remediation hint."""
    return rule_catalogue()
