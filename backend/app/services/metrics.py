"""Aggregation helpers over the page-level metric tables.

Both the priority engine and the dashboard need "the last N days for these pages", so the query
lives here once. Everything is aggregated in SQL — pulling per-day rows into Python would not
survive a 10 000-page site with a 28-day window.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GA4Metric, GSCMetric, SemrushMetric

EMPTY_METRICS: dict[str, Any] = {
    "users": 0,
    "sessions": 0,
    "engagement_rate": None,
    "conversions": 0.0,
    "revenue": 0.0,
    "clicks": 0,
    "impressions": 0,
    "ctr": None,
    "position": None,
    "organic_keywords": 0,
    "organic_traffic": 0,
    "striking_distance_keywords": 0,
    "opportunity_volume": 0,
    "backlinks": 0,
}


def window_start(window_days: int | None = None, *, today: date | None = None) -> date:
    days = window_days or settings.priority_metric_window_days
    return (today or date.today()) - timedelta(days=days)


def _chunks(values: Sequence[int], size: int = 900) -> Iterable[Sequence[int]]:
    """SQLite caps bound parameters at ~999; chunk long id lists to stay under it."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def aggregate_page_metrics(
    db: Session,
    page_ids: Sequence[int],
    *,
    window_days: int | None = None,
    today: date | None = None,
) -> dict[int, dict[str, Any]]:
    """Return ``{page_id: metrics}`` summed/averaged across the lookback window.

    Pages with no metric rows are returned with zeroed values so callers never need a null check.
    """
    result: dict[int, dict[str, Any]] = {pid: dict(EMPTY_METRICS) for pid in page_ids}
    if not page_ids:
        return result

    since = window_start(window_days, today=today)

    for chunk in _chunks(list(page_ids)):
        # ── GA4: user activity and business value ───────────────────────────
        ga4_rows = db.execute(
            select(
                GA4Metric.page_id,
                func.sum(GA4Metric.users),
                func.sum(GA4Metric.sessions),
                func.avg(GA4Metric.engagement_rate),
                func.sum(GA4Metric.conversions),
                func.sum(GA4Metric.revenue),
            )
            .where(GA4Metric.page_id.in_(chunk), GA4Metric.date >= since)
            .group_by(GA4Metric.page_id)
        ).all()
        for page_id, users, sessions, engagement, conversions, revenue in ga4_rows:
            entry = result[page_id]
            entry["users"] = int(users or 0)
            entry["sessions"] = int(sessions or 0)
            entry["engagement_rate"] = float(engagement) if engagement is not None else None
            entry["conversions"] = float(conversions or 0.0)
            entry["revenue"] = float(revenue or 0.0)

        # ── GSC: search demand and current visibility ───────────────────────
        gsc_rows = db.execute(
            select(
                GSCMetric.page_id,
                func.sum(GSCMetric.clicks),
                func.sum(GSCMetric.impressions),
                func.avg(GSCMetric.position),
            )
            .where(GSCMetric.page_id.in_(chunk), GSCMetric.date >= since)
            .group_by(GSCMetric.page_id)
        ).all()
        for page_id, clicks, impressions, position in gsc_rows:
            entry = result[page_id]
            entry["clicks"] = int(clicks or 0)
            entry["impressions"] = int(impressions or 0)
            entry["position"] = round(float(position), 1) if position is not None else None
            # Derive CTR from the totals rather than averaging daily CTRs, which would
            # over-weight low-impression days.
            entry["ctr"] = (
                round(entry["clicks"] / entry["impressions"], 4) if entry["impressions"] else None
            )

        # ── Semrush: opportunity. Only the latest snapshot is meaningful. ────
        latest_dates = (
            select(SemrushMetric.page_id, func.max(SemrushMetric.date).label("max_date"))
            .where(SemrushMetric.page_id.in_(chunk), SemrushMetric.date >= since)
            .group_by(SemrushMetric.page_id)
            .subquery()
        )
        semrush_rows = db.execute(
            select(
                SemrushMetric.page_id,
                SemrushMetric.organic_keywords,
                SemrushMetric.organic_traffic,
                SemrushMetric.striking_distance_keywords,
                SemrushMetric.opportunity_volume,
                SemrushMetric.backlinks,
            ).join(
                latest_dates,
                (SemrushMetric.page_id == latest_dates.c.page_id)
                & (SemrushMetric.date == latest_dates.c.max_date),
            )
        ).all()
        for page_id, keywords, traffic, striking, volume, backlinks in semrush_rows:
            entry = result[page_id]
            entry["organic_keywords"] = int(keywords or 0)
            entry["organic_traffic"] = int(traffic or 0)
            entry["striking_distance_keywords"] = int(striking or 0)
            entry["opportunity_volume"] = int(volume or 0)
            entry["backlinks"] = int(backlinks or 0)

    return result


def page_history(
    db: Session, page_id: int, *, days: int = 90, today: date | None = None
) -> list[dict[str, Any]]:
    """Daily time series for one page, merging every provider onto a single date axis."""
    since = (today or date.today()) - timedelta(days=days)
    series: dict[date, dict[str, Any]] = {}

    def slot(day: date) -> dict[str, Any]:
        return series.setdefault(
            day,
            {
                "date": day,
                "seo_score": None,
                "priority_score": None,
                "issue_count": 0,
                "clicks": 0,
                "impressions": 0,
                "users": 0,
                "sessions": 0,
                "conversions": 0.0,
                "revenue": 0.0,
            },
        )

    for row in db.execute(
        select(GSCMetric.date, GSCMetric.clicks, GSCMetric.impressions).where(
            GSCMetric.page_id == page_id, GSCMetric.date >= since
        )
    ).all():
        entry = slot(row[0])
        entry["clicks"] = int(row[1] or 0)
        entry["impressions"] = int(row[2] or 0)

    for row in db.execute(
        select(
            GA4Metric.date, GA4Metric.users, GA4Metric.sessions,
            GA4Metric.conversions, GA4Metric.revenue,
        ).where(GA4Metric.page_id == page_id, GA4Metric.date >= since)
    ).all():
        entry = slot(row[0])
        entry["users"] = int(row[1] or 0)
        entry["sessions"] = int(row[2] or 0)
        entry["conversions"] = float(row[3] or 0.0)
        entry["revenue"] = float(row[4] or 0.0)

    from ..models import HistoricalMetric

    for row in db.execute(
        select(
            HistoricalMetric.date,
            HistoricalMetric.seo_score,
            HistoricalMetric.priority_score,
            HistoricalMetric.issue_count,
        ).where(HistoricalMetric.page_id == page_id, HistoricalMetric.date >= since)
    ).all():
        entry = slot(row[0])
        entry["seo_score"] = float(row[1]) if row[1] is not None else None
        entry["priority_score"] = float(row[2]) if row[2] is not None else None
        entry["issue_count"] = int(row[3] or 0)

    return [series[day] for day in sorted(series)]
