"""Celery application, queue topology and the nightly schedule.

This module owns configuration only — tasks live in :mod:`app.services.jobs.tasks` — so importing
it never drags in the whole service layer.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from .config import settings

celery_app = Celery(
    "seo_automation",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.services.jobs.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # A crawl can run for an hour; losing it to an early ack would leave a run stuck at "running".
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Long, uneven tasks must not be pre-assigned to a busy worker while another sits idle.
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,
    broker_connection_retry_on_startup=True,
    result_expires=60 * 60 * 24,
    task_soft_time_limit=settings.crawl_time_budget_seconds,
    task_time_limit=settings.crawl_time_budget_seconds + 300,
    # Separate queues stop a multi-hour crawl from starving quick metric syncs. Run one worker per
    # queue in production and scale them independently.
    task_routes={
        "seo.crawl.*": {"queue": "crawl"},
        "seo.sync.*": {"queue": "sync"},
        "seo.score.*": {"queue": "score"},
        "seo.ai.*": {"queue": "ai"},
    },
)

#: Nightly cadence. Syncs land first, priority is recomputed against the fresh metrics, and the
#: rollup snapshots the result — so the order of the hours here is deliberate, not arbitrary.
celery_app.conf.beat_schedule = {
    "daily-provider-sync": {
        "task": "seo.sync.daily_all",
        "schedule": crontab(hour=settings.daily_sync_hour_utc, minute=0),
    },
    "daily-priority-scoring": {
        "task": "seo.score.daily_all",
        "schedule": crontab(hour=(settings.daily_sync_hour_utc + 2) % 24, minute=0),
    },
    "daily-history-rollup": {
        "task": "seo.score.rollup",
        "schedule": crontab(hour=(settings.daily_sync_hour_utc + 3) % 24, minute=0),
    },
    "daily-scheduled-crawl": {
        "task": "seo.crawl.daily_all",
        "schedule": crontab(hour=settings.scheduled_crawl_hour_utc, minute=30),
    },
}
