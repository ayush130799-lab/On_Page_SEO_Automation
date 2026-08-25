"""Daily rollups into ``historical_metrics``.

Provider metric tables have retention windows and are re-synced, so they cannot be the record of
how a score moved. This table is written by the platform itself and is therefore the durable
history behind the trend charts.

Rollups are idempotent: running twice for the same day overwrites rather than duplicates.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..models import (
    GA4Metric,
    GSCMetric,
    HistoricalMetric,
    Page,
    SEOIssue,
    Severity,
    Website,
)

logger = logging.getLogger(__name__)

#: Rolling up every page daily on a 10 000-page portfolio would add millions of rows a year, so
#: only the pages that matter get a per-page series. The website series always covers everything.
MAX_TRACKED_PAGES_PER_SITE = 500


def _upsert(db: Session, **values: Any) -> None:
    existing = db.scalar(
        select(HistoricalMetric).where(
            HistoricalMetric.website_id == values["website_id"],
            HistoricalMetric.page_id.is_(None)
            if values.get("page_id") is None
            else HistoricalMetric.page_id == values["page_id"],
            HistoricalMetric.date == values["date"],
            HistoricalMetric.scope == values["scope"],
        )
    )
    if existing is None:
        db.add(HistoricalMetric(**values))
        return
    for key, value in values.items():
        setattr(existing, key, value)


def rollup_website(db: Session, website: Website, day: date | None = None) -> dict[str, Any]:
    """Snapshot one website's scores and traffic for a single day."""
    target = day or date.today()
    active = Page.is_active.is_(True)

    page_count, average_seo, average_priority = db.execute(
        select(
            func.count(Page.id),
            func.avg(Page.seo_score),
            func.avg(Page.priority_score),
        ).where(Page.website_id == website.id, active)
    ).one()

    issue_total, critical_total = db.execute(
        select(
            func.count(SEOIssue.id),
            func.sum(case((SEOIssue.severity == Severity.CRITICAL, 1), else_=0)),
        )
        .join(Page, SEOIssue.page_id == Page.id)
        .where(Page.website_id == website.id, active, SEOIssue.is_resolved.is_(False))
    ).one()

    clicks, impressions = db.execute(
        select(
            func.coalesce(func.sum(GSCMetric.clicks), 0),
            func.coalesce(func.sum(GSCMetric.impressions), 0),
        ).where(GSCMetric.website_id == website.id, GSCMetric.date == target)
    ).one()

    users, sessions, conversions, revenue = db.execute(
        select(
            func.coalesce(func.sum(GA4Metric.users), 0),
            func.coalesce(func.sum(GA4Metric.sessions), 0),
            func.coalesce(func.sum(GA4Metric.conversions), 0.0),
            func.coalesce(func.sum(GA4Metric.revenue), 0.0),
        ).where(GA4Metric.website_id == website.id, GA4Metric.date == target)
    ).one()

    _upsert(
        db,
        website_id=website.id,
        page_id=None,
        date=target,
        scope="website",
        seo_score=round(float(average_seo), 1) if average_seo is not None else None,
        priority_score=round(float(average_priority), 1) if average_priority is not None else None,
        issue_count=int(issue_total or 0),
        critical_count=int(critical_total or 0),
        page_count=int(page_count or 0),
        clicks=int(clicks),
        impressions=int(impressions),
        users=int(users),
        sessions=int(sessions),
        conversions=float(conversions),
        revenue=float(revenue),
    )

    tracked = db.scalars(
        select(Page)
        .where(Page.website_id == website.id, active, Page.priority_score.isnot(None))
        .order_by(Page.priority_score.desc())
        .limit(MAX_TRACKED_PAGES_PER_SITE)
    ).all()

    for page in tracked:
        _upsert(
            db,
            website_id=website.id,
            page_id=page.id,
            date=target,
            scope="page",
            seo_score=page.seo_score,
            priority_score=page.priority_score,
            issue_count=page.issue_count,
            critical_count=1 if page.highest_severity == Severity.CRITICAL else 0,
            page_count=1,
        )

    db.commit()

    summary = {
        "website_id": website.id,
        "date": target.isoformat(),
        "pages": int(page_count or 0),
        "tracked_pages": len(tracked),
        "average_seo_score": round(float(average_seo), 1) if average_seo is not None else None,
        "issues": int(issue_total or 0),
    }
    logger.info("Rolled up website %s for %s: %s", website.id, target, summary)
    return summary


def rollup_all(db: Session, day: date | None = None) -> list[dict[str, Any]]:
    """Roll up every active website — the nightly beat task."""
    websites = db.scalars(select(Website).where(Website.is_active.is_(True))).all()
    results = []
    for website in websites:
        try:
            results.append(rollup_website(db, website, day))
        except Exception as exc:
            # One bad website must not abort the whole nightly job.
            db.rollback()
            logger.exception("Rollup failed for website %s: %s", website.id, exc)
    return results


def prune_history(db: Session, keep_days: int = 730) -> int:
    """Drop rollups older than the retention window."""
    cutoff = date.today() - timedelta(days=keep_days)
    deleted = (
        db.query(HistoricalMetric).filter(HistoricalMetric.date < cutoff).delete(
            synchronize_session=False
        )
    )
    db.commit()
    if deleted:
        logger.info("Pruned %d historical rows older than %s.", deleted, cutoff)
    return deleted
