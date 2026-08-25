"""Background-job visibility."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from ...config import settings
from ...core.deps import AdminUser, CurrentUser, DbSession, ReadableWebsite, accessible_website_ids
from ...core.errors import NotFoundError
from ...models import Job, RunStatus

router = APIRouter(prefix="/api", tags=["jobs"])


def _serialise(job: Job) -> dict:
    return {
        "id": job.id,
        "website_id": job.website_id,
        "job_type": job.job_type,
        "status": job.status,
        "queue": job.queue,
        "progress_percent": job.progress_percent,
        "progress_message": job.progress_message,
        "items_total": job.items_total,
        "items_done": job.items_done,
        "items_failed": job.items_failed,
        "attempts": job.attempts,
        "result": job.result,
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_seconds": job.duration_seconds,
        "created_at": job.created_at,
    }


@router.get("/websites/{website_id}/jobs")
def list_website_jobs(
    website: ReadableWebsite,
    db: DbSession,
    limit: int = Query(25, ge=1, le=200),
    status: str | None = None,
):
    """Recent background work for one website."""
    stmt = select(Job).where(Job.website_id == website.id)
    if status:
        stmt = stmt.where(Job.status == status.lower())
    jobs = db.scalars(stmt.order_by(Job.id.desc()).limit(limit)).all()
    return {"items": [_serialise(job) for job in jobs]}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, user: CurrentUser, db: DbSession):
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} was not found.")

    if job.website_id is not None:
        from ...core.deps import get_website_for_read

        get_website_for_read(job.website_id, user, db)
    else:
        # Platform-wide jobs (the nightly rollup) belong to no website, so only an administrator
        # has a basis for seeing them.
        from ...core.deps import require_admin

        require_admin(user)

    return _serialise(job)


@router.get("/jobs")
def list_jobs(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(50, ge=1, le=200),
    status: str | None = None,
    job_type: str | None = None,
):
    """Jobs across every website the caller can see."""
    stmt = select(Job)
    allowed = accessible_website_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Job.website_id.in_(allowed or [-1]))
    if status:
        stmt = stmt.where(Job.status == status.lower())
    if job_type:
        stmt = stmt.where(Job.job_type == job_type.lower())

    jobs = db.scalars(stmt.order_by(Job.id.desc()).limit(limit)).all()
    return {"items": [_serialise(job) for job in jobs]}


@router.get("/system/health")
def system_health(db: DbSession, _: AdminUser):
    """Operational snapshot: queue health, worker reachability and recent failures.

    Exposed for administrators (and uptime checks) because "the dashboard looks stale" is almost
    always a worker or broker problem, and that should be answerable without shell access.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    counts = dict(
        db.execute(
            select(Job.status, func.count(Job.id)).where(Job.created_at >= since)
            .group_by(Job.status)
        ).all()
    )

    stuck = db.scalar(
        select(func.count(Job.id)).where(
            Job.status == RunStatus.RUNNING,
            Job.started_at < datetime.now(timezone.utc) - timedelta(hours=2),
        )
    )

    broker = {"reachable": False, "detail": "not checked"}
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        broker = {"reachable": True, "detail": "ok"}
    except Exception as exc:
        broker = {"reachable": False, "detail": f"{type(exc).__name__}"}

    recent_failures = [
        {"id": job.id, "job_type": job.job_type, "error": job.error, "at": job.finished_at}
        for job in db.scalars(
            select(Job)
            .where(Job.status == RunStatus.FAILED, Job.created_at >= since)
            .order_by(Job.id.desc())
            .limit(10)
        )
    ]

    return {
        "jobs_last_24h": counts,
        "stuck_jobs": stuck or 0,
        "broker": broker,
        "recent_failures": recent_failures,
    }
