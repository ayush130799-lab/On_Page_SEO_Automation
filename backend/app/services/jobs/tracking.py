"""Durable job tracking and distributed locking.

Celery tracks task state in its result backend, but that state is ephemeral, invisible to the
dashboard and gone after ``result_expires``. The ``jobs`` table is the queryable record the UI
reads and an operator greps when something did not run.

The lock exists because the expensive operations here are not safe to double-run: two crawls of the
same website at once double the outbound traffic and race each other's writes.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy.orm import Session

from ...config import settings
from ...models import Job, RunStatus

logger = logging.getLogger(__name__)

#: A lock must outlive the work it guards, but not so long that a crashed worker blocks a website
#: indefinitely. Slightly longer than the crawl time budget.
DEFAULT_LOCK_TTL_SECONDS = 3900


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Job records ─────────────────────────────────────────────────────────────


def start_job(
    db: Session,
    job_type: str,
    *,
    website_id: int | None = None,
    queue: str | None = None,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    items_total: int = 0,
) -> Job:
    """Record a job as running. ``payload`` must never contain credentials."""
    job = Job(
        website_id=website_id,
        job_type=job_type,
        status=RunStatus.RUNNING,
        queue=queue,
        task_id=task_id,
        payload=payload,
        items_total=items_total,
        started_at=_now(),
        attempts=1,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def update_progress(
    db: Session,
    job: Job,
    *,
    items_done: int | None = None,
    items_failed: int | None = None,
    message: str | None = None,
) -> None:
    if items_done is not None:
        job.items_done = items_done
    if items_failed is not None:
        job.items_failed = items_failed
    if message is not None:
        job.progress_message = message[:255]
    if job.items_total:
        job.progress_percent = round(min(100.0, 100.0 * job.items_done / job.items_total), 1)
    db.commit()


def finish_job(db: Session, job: Job, result: dict[str, Any] | None = None) -> None:
    job.status = RunStatus.COMPLETED
    job.result = result
    job.progress_percent = 100.0
    job.finished_at = _now()
    if job.started_at:
        job.duration_seconds = round((job.finished_at - job.started_at).total_seconds(), 2)
    db.commit()


def fail_job(db: Session, job: Job, error: str) -> None:
    from ...core.logging import redact

    job.status = RunStatus.FAILED
    job.error = redact(error)[:2000]
    job.finished_at = _now()
    if job.started_at:
        job.duration_seconds = round((job.finished_at - job.started_at).total_seconds(), 2)
    db.commit()


@contextlib.contextmanager
def tracked_job(
    db: Session,
    job_type: str,
    *,
    website_id: int | None = None,
    queue: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Iterator[Job]:
    """Run a block as a tracked job, recording success or failure either way."""
    job = start_job(db, job_type, website_id=website_id, queue=queue, payload=payload)
    try:
        yield job
    except Exception as exc:
        db.rollback()
        fail_job(db, job, f"{type(exc).__name__}: {exc}")
        raise
    else:
        if job.status == RunStatus.RUNNING:
            # Pass the existing result through: callers set `job.result` inside the block, and
            # calling finish_job with the default would overwrite it with None.
            finish_job(db, job, job.result)


# ── Distributed locking ─────────────────────────────────────────────────────


class LockUnavailable(RuntimeError):
    """Another worker already holds the lock for this resource."""


def _redis_client():
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        return client
    except Exception as exc:
        logger.debug("Redis lock unavailable (%s); proceeding without one.", exc)
        return None


@contextlib.contextmanager
def website_lock(
    website_id: int, operation: str, ttl: int = DEFAULT_LOCK_TTL_SECONDS
) -> Iterator[bool]:
    """Hold an exclusive lock on one operation for one website.

    Yields ``True`` when the lock was acquired. Without Redis it yields ``True`` unconditionally —
    a single-process deployment cannot race with itself, and the API-level guard against concurrent
    crawls still applies.
    """
    client = _redis_client()
    if client is None:
        yield True
        return

    key = f"lock:{operation}:website:{website_id}"
    token = uuid.uuid4().hex

    acquired = False
    try:
        acquired = bool(client.set(key, token, nx=True, ex=ttl))
        if not acquired:
            logger.info("Skipping %s for website %s: another worker holds the lock.",
                        operation, website_id)
        yield acquired
    finally:
        if acquired:
            try:
                # Release only our own lock: a lock that expired mid-run may now belong to
                # someone else, and deleting it blindly would let a third worker in.
                if client.get(key) == token.encode():
                    client.delete(key)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Could not release the %s lock: %s", operation, exc)
