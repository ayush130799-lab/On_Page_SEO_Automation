"""Celery task definitions.

Tasks are thin: they own a database session, take a lock where double-running would be harmful,
record a ``Job`` row, and delegate into the service layer — so the same code path runs identically
under Celery, under FastAPI background tasks and in tests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...celery_app import celery_app
from ...db import SessionLocal
from ...models import JobType
from .tracking import tracked_job, website_lock

logger = logging.getLogger(__name__)


@celery_app.task(name="seo.crawl.run", bind=True, max_retries=2, default_retry_delay=30)
def run_crawl_task(self, crawl_run_id: int) -> dict[str, Any]:
    """Execute one crawl run."""
    from ...models import CrawlRun
    from ..pipeline import run_crawl_pipeline

    db = SessionLocal()
    try:
        crawl_run = db.get(CrawlRun, crawl_run_id)
        if crawl_run is None:
            return {"status": "skipped", "reason": "crawl_run_not_found"}

        with website_lock(crawl_run.website_id, "crawl") as acquired:
            if not acquired:
                return {"status": "skipped", "reason": "another_crawl_in_progress"}

            with tracked_job(
                db,
                JobType.CRAWL,
                website_id=crawl_run.website_id,
                queue="crawl",
                payload={"crawl_run_id": crawl_run_id},
            ) as job:
                outcome = asyncio.run(run_crawl_pipeline(db, crawl_run_id))
                result = {
                    "crawl_run_id": outcome.crawl_run_id,
                    "pages_crawled": outcome.pages_crawled,
                    "pages_audited": outcome.pages_audited,
                    "issues_created": outcome.issues_created,
                    "average_seo_score": outcome.average_seo_score,
                }
                job.result = result
                return result
    except Exception as exc:
        logger.exception("Crawl task failed for run %s: %s", crawl_run_id, exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.sync.provider", bind=True, max_retries=3, default_retry_delay=120)
def run_sync_task(self, website_id: int, provider: str, days: int | None = None) -> dict[str, Any]:
    """Pull metrics for one website from one provider."""
    from ...models import IntegrationProvider, Website
    from ..integrations import ga4, gsc, semrush

    job_types = {
        IntegrationProvider.GSC: JobType.GSC_SYNC,
        IntegrationProvider.GA4: JobType.GA4_SYNC,
        IntegrationProvider.SEMRUSH: JobType.SEMRUSH_SYNC,
    }

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return {"status": "skipped", "reason": "website_not_found"}

        with website_lock(website_id, f"sync:{provider}", ttl=1800) as acquired:
            if not acquired:
                return {"status": "skipped", "reason": "sync_already_running"}

            with tracked_job(
                db,
                job_types.get(provider, JobType.GSC_SYNC),
                website_id=website_id,
                queue="sync",
                payload={"provider": provider, "days": days},
            ) as job:
                if provider == IntegrationProvider.GSC:
                    result = asyncio.run(gsc.sync(db, website, days=days))
                elif provider == IntegrationProvider.GA4:
                    result = asyncio.run(ga4.sync(db, website, days=days))
                elif provider == IntegrationProvider.SEMRUSH:
                    result = asyncio.run(semrush.sync(db, website))
                else:
                    return {"status": "skipped", "reason": f"unknown_provider:{provider}"}

                website.last_synced_at = job.started_at
                db.commit()

                # Re-score priority scores with newly imported metrics
                from ..priority import score_website
                score_website(db, website)

                job.result = result
                return result
    except Exception as exc:
        logger.exception("%s sync failed for website %s: %s", provider, website_id, exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.score.priority", bind=True, max_retries=2, default_retry_delay=60)
def run_scoring_task(self, website_id: int, window_days: int | None = None) -> dict[str, Any]:
    """Recompute priority scores for one website."""
    from ...models import Website
    from ..priority import score_website

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return {"status": "skipped", "reason": "website_not_found"}

        with tracked_job(
            db, JobType.PRIORITY_SCORING, website_id=website_id, queue="score"
        ) as job:
            result = score_website(db, website, window_days=window_days)
            payload = {
                "website_id": website_id,
                "pages_scored": result.pages_scored,
                "weights": result.weights,
                "data_sources": result.data_sources,
            }
            job.result = payload
            return payload
    except Exception as exc:
        logger.exception("Priority scoring failed for website %s: %s", website_id, exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.github.analyse_pr", bind=True, max_retries=2, default_retry_delay=30)
def run_github_pr_analysis_task(
    self, website_id: int, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Analyse one pull_request webhook delivery: diff, predict, comment, gate.

    Split from the webhook route so a slow or rate-limited GitHub API call never blocks the
    webhook response — GitHub's own delivery UI treats a slow response as a near-failure and
    will retry it, which would otherwise risk double-posting the same PR comment.
    """
    from ...models import Website
    from ..github.pr_handler import process_pull_request

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return {"status": "skipped", "reason": "website_not_found"}

        with tracked_job(
            db, JobType.GITHUB_PR_ANALYSIS, website_id=website_id, queue="ai",
            payload={"event_type": event_type, "pr_number": payload.get("number")},
        ) as job:
            outcome = asyncio.run(
                process_pull_request(db, event_type=event_type, payload=payload, website=website)
            )
            result = {
                "website_id": website_id,
                "action": outcome.action,
                "reason": outcome.reason,
                "risk_level": outcome.risk_level,
                "expected_impact": outcome.expected_impact,
                "comment_posted": outcome.comment_posted,
            }
            job.result = result
            return result
    except Exception as exc:
        logger.exception(
            "GitHub PR analysis failed for website %s: %s", website_id, exc
        )
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.serp.analyse_competitors", bind=True, max_retries=1, default_retry_delay=30)
def run_competitor_analysis_task(
    self, website_id: int, keyword: str, page_id: int | None = None
) -> dict[str, Any]:
    """Run one SerpApi lookup plus up to N competitor-page fetches for a keyword.

    max_retries=1 (not the usual 2-3): a failed SerpApi call or a blocked competitor fetch is
    rarely transient in the way a database hiccup is, and retrying a paid SerpApi call
    automatically risks double-billing for what is often a genuine, non-retryable failure
    (invalid key, exhausted quota).
    """
    from ...models import Website
    from ..serp import analyse_competitors

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return {"status": "skipped", "reason": "website_not_found"}

        with tracked_job(
            db, JobType.COMPETITOR_ANALYSIS, website_id=website_id, queue="ai",
            payload={"keyword": keyword, "page_id": page_id},
        ) as job:
            outcome = asyncio.run(
                analyse_competitors(db, website, keyword=keyword, page_id=page_id)
            )
            result = {
                "website_id": website_id,
                "analysis_id": outcome.analysis_id,
                "keyword": outcome.keyword,
                "fetched_count": outcome.fetched_count,
                "failed_count": outcome.failed_count,
                "paa_count": outcome.paa_count,
                "error": outcome.error,
            }
            job.result = result
            return result
    except Exception as exc:
        logger.exception(
            "Competitor analysis failed for website %s, keyword %r: %s", website_id, keyword, exc
        )
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.impact.rescore", bind=True, max_retries=2, default_retry_delay=60)
def run_impact_rescore_task(self, website_id: int) -> dict[str, Any]:
    """Refresh intent classification and impact-ranked recommendations for one website.

    Runs after the nightly GSC/GA4 sync and priority scoring, without a crawl. GSC clicks and
    impressions change daily; a recommendation that was P2 last week can become P0 today purely
    because its click-through rate moved. Without this task, impact scores and keyword tiers only
    updated on the next full crawl, which could be days away.
    """
    from ...models import Website
    from ..impact.engine import score_website_recommendations
    from ..intent import analyse_intent_for_website

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return {"status": "skipped", "reason": "website_not_found"}

        with tracked_job(db, JobType.PRIORITY_SCORING, website_id=website_id, queue="score") as job:
            intent_outcome = analyse_intent_for_website(db, website)
            db.commit()
            scoring_outcome = score_website_recommendations(db, website)
            db.commit()

            payload = {
                "website_id": website_id,
                "intent_classified": intent_outcome.classified,
                "intent_mismatches": intent_outcome.mismatches_found,
                "recommendations_written": scoring_outcome.recommendations_written,
                "tier_counts": scoring_outcome.tier_counts,
                "priority_counts": scoring_outcome.priority_counts,
            }
            job.result = payload
            return payload
    except Exception as exc:
        db.rollback()
        logger.exception("Impact rescore failed for website %s: %s", website_id, exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.ai.analyse", bind=True, max_retries=1, default_retry_delay=120)
def run_ai_task(self, website_id: int, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the AI recommendation stage for one website."""
    from ...models import Website
    from ..ai import analyse_website

    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return {"status": "skipped", "reason": "website_not_found"}

        with website_lock(website_id, "ai", ttl=1800) as acquired:
            if not acquired:
                return {"status": "skipped", "reason": "analysis_already_running"}

            with tracked_job(
                db, JobType.AI_ANALYSIS, website_id=website_id, queue="ai", payload=options
            ) as job:
                outcome = asyncio.run(analyse_website(db, website, **(options or {})))
                result = {
                    "website_id": website_id,
                    "analysed": outcome.analysed,
                    "cached": outcome.cached,
                    "skipped": outcome.skipped,
                    "failed": outcome.failed,
                    "provider": outcome.provider,
                }
                job.result = result
                return result
    except Exception as exc:
        logger.exception("AI analysis failed for website %s: %s", website_id, exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.score.rollup", bind=True, max_retries=1)
def run_rollup_task(self) -> dict[str, Any]:
    """Nightly snapshot of every website's scores and traffic."""
    from ..rollup import prune_history, rollup_all

    db = SessionLocal()
    try:
        with tracked_job(db, JobType.ROLLUP, queue="score") as job:
            results = rollup_all(db)
            pruned = prune_history(db)
            payload = {"websites": len(results), "pruned_rows": pruned}
            job.result = payload
            return payload
    except Exception as exc:
        logger.exception("Historical rollup failed: %s", exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.experiments.check_due", bind=True, max_retries=1)
def run_due_experiment_checkpoints_task(self) -> dict[str, Any]:
    """§8.4: measure every SEO experiment checkpoint whose due date has arrived, across every
    website. Extends the GSC/GA4 pipeline rather than a separate subsystem, so it runs on the
    same daily cadence as the rest of the metrics/scoring chain."""
    from ..experiments import run_due_checkpoints

    db = SessionLocal()
    try:
        with tracked_job(db, JobType.EXPERIMENT_CHECKPOINT, queue="score") as job:
            outcome = run_due_checkpoints(db)
            payload = {
                "measured": outcome.measured,
                "experiments_completed": outcome.experiments_completed,
                "errors": outcome.errors[:10],
            }
            job.result = payload
            return payload
    except Exception as exc:
        logger.exception("Experiment checkpoint sweep failed: %s", exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(name="seo.sync.daily_all")
def sync_all_websites() -> dict[str, Any]:
    """Queue a metric sync for every connected integration across the portfolio."""
    from sqlalchemy import select

    from ...models import Integration, IntegrationProvider, IntegrationStatus

    syncable = (IntegrationProvider.GSC, IntegrationProvider.GA4, IntegrationProvider.SEMRUSH)

    db = SessionLocal()
    try:
        rows = db.execute(
            select(Integration.website_id, Integration.provider).where(
                Integration.provider.in_(syncable),
                Integration.status == IntegrationStatus.CONNECTED,
            )
        ).all()
        for website_id, provider in rows:
            run_sync_task.delay(website_id, provider, None)
        logger.info("Queued %d provider syncs.", len(rows))
        return {"queued": len(rows)}
    finally:
        db.close()


@celery_app.task(name="seo.crawl.daily_all")
def crawl_all_websites() -> dict[str, Any]:
    """Queue a scheduled crawl for every active website."""
    from sqlalchemy import select

    from ...config import settings
    from ...models import CrawlRun, CrawlTrigger, RunStatus, Website
    from ..pipeline import create_crawl_run

    if not settings.scheduled_crawl_enabled:
        return {"queued": 0, "reason": "scheduled crawling is disabled"}

    db = SessionLocal()
    try:
        websites = db.scalars(select(Website).where(Website.is_active.is_(True))).all()
        queued = 0
        for website in websites:
            active = db.scalar(
                select(CrawlRun.id).where(
                    CrawlRun.website_id == website.id,
                    CrawlRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
                )
            )
            if active is not None:
                continue
            run = create_crawl_run(db, website, trigger=CrawlTrigger.SCHEDULED)
            run_crawl_task.delay(run.id)
            queued += 1
        logger.info("Queued %d scheduled crawls.", queued)
        return {"queued": queued}
    finally:
        db.close()


@celery_app.task(name="seo.score.daily_all")
def score_all_websites() -> dict[str, Any]:
    """Recompute priority for every website, after the nightly syncs have landed."""
    from sqlalchemy import select

    from ...models import Website

    db = SessionLocal()
    try:
        website_ids = db.scalars(
            select(Website.id).where(Website.is_active.is_(True))
        ).all()
        for website_id in website_ids:
            run_scoring_task.delay(website_id, None)
            # Chained rather than parallel scheduling: impact scoring reads the priority score
            # and intent depends on the GSC queries the sync stage just wrote, so both should
            # trail the scoring task rather than race it. Celery has no built-in "after this
            # other task" primitive without chains, so this queues a fixed delay instead.
            run_impact_rescore_task.apply_async(args=[website_id], countdown=120)
        return {"queued": len(website_ids)}
    finally:
        db.close()
