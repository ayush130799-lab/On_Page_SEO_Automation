"""Dashboard aggregates.

These endpoints exist so the UI makes one request per screen instead of stitching a dozen
resources together in the browser. Every aggregate is computed in SQL — a portfolio of 50 sites
with 10 000 pages each cannot be summarised by loading rows into Python.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ...config import settings
from ...core.deps import CurrentUser, DbSession, ReadableWebsite, accessible_website_ids
from ...models import (
    AIRecommendation,
    AIStatus,
    CrawlRun,
    GA4Metric,
    GSCMetric,
    HistoricalMetric,
    Integration,
    IntegrationProvider,
    Page,
    RunStatus,
    SEOIssue,
    Severity,
    Website,
    severity_rank,
)
from ...services.metrics import window_start
from ...services.priority import available_data_sources

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

ALL_PROVIDERS = (
    IntegrationProvider.GSC,
    IntegrationProvider.GA4,
    IntegrationProvider.SEMRUSH,
    IntegrationProvider.GITHUB,
)


def _integration_map(db: Session, website_ids: list[int]) -> dict[int, dict[str, str]]:
    """``{website_id: {provider: status}}`` for every supported provider."""
    result = {wid: {p: "not_connected" for p in ALL_PROVIDERS} for wid in website_ids}
    if not website_ids:
        return result

    for website_id, provider, status in db.execute(
        select(Integration.website_id, Integration.provider, Integration.status).where(
            Integration.website_id.in_(website_ids)
        )
    ):
        result.setdefault(website_id, {})[provider] = status
    return result


@router.get("/overview")
def portfolio_overview(user: CurrentUser, db: DbSession, window_days: int | None = Query(None)):
    """Everything the portfolio screen shows, across every website the caller can see.

    This is the answer to *"across all websites, where should we start?"* — sites are ordered by
    how much high-priority work they carry, not alphabetically.
    """
    window = window_days or settings.priority_metric_window_days
    since = window_start(window)

    allowed = accessible_website_ids(db, user)
    stmt = select(Website)
    if allowed is not None:
        stmt = stmt.where(Website.id.in_(allowed or [-1]))
    websites = db.scalars(stmt).all()
    website_ids = [w.id for w in websites]

    integrations = _integration_map(db, website_ids)

    # Issue counts by severity, per website, in one query.
    issue_rows = db.execute(
        select(Page.website_id, SEOIssue.severity, func.count(SEOIssue.id))
        .join(SEOIssue, SEOIssue.page_id == Page.id)
        .where(
            Page.website_id.in_(website_ids or [-1]),
            Page.is_active.is_(True),
            SEOIssue.is_resolved.is_(False),
        )
        .group_by(Page.website_id, SEOIssue.severity)
    ).all()
    issues_by_site: dict[int, dict[str, int]] = {}
    for website_id, severity, count in issue_rows:
        issues_by_site.setdefault(website_id, {})[severity] = count

    # Priority-band counts per website.
    band_rows = db.execute(
        select(Page.website_id, Page.priority_band, func.count(Page.id))
        .where(Page.website_id.in_(website_ids or [-1]), Page.is_active.is_(True))
        .group_by(Page.website_id, Page.priority_band)
    ).all()
    bands_by_site: dict[int, dict[str, int]] = {}
    for website_id, band, count in band_rows:
        if band:
            bands_by_site.setdefault(website_id, {})[band] = count

    # Traffic totals over the window.
    traffic_rows = db.execute(
        select(
            GA4Metric.website_id,
            func.sum(GA4Metric.users),
            func.sum(GA4Metric.sessions),
            func.sum(GA4Metric.conversions),
            func.sum(GA4Metric.revenue),
        )
        .where(GA4Metric.website_id.in_(website_ids or [-1]), GA4Metric.date >= since)
        .group_by(GA4Metric.website_id)
    ).all()
    traffic_by_site = {}
    for website_id in website_ids:
        ga4_status = integrations.get(website_id, {}).get(IntegrationProvider.GA4, "not_connected")
        row = next((r for r in traffic_rows if r[0] == website_id), None)
        if row and (row[1] is not None or row[2] is not None):
            traffic_by_site[website_id] = {
                "status": ga4_status,
                "users": int(row[1] or 0),
                "sessions": int(row[2] or 0),
                "conversions": float(row[3] or 0),
                "revenue": float(row[4] or 0),
            }
        elif ga4_status == "connected":
            traffic_by_site[website_id] = {
                "status": "connected",
                "users": 0,
                "sessions": 0,
                "conversions": 0.0,
                "revenue": 0.0,
            }
        else:
            traffic_by_site[website_id] = {
                "status": ga4_status,
                "users": None,
                "sessions": None,
                "conversions": None,
                "revenue": None,
            }

    search_rows = db.execute(
        select(
            GSCMetric.website_id,
            func.sum(GSCMetric.clicks),
            func.sum(GSCMetric.impressions),
        )
        .where(GSCMetric.website_id.in_(website_ids or [-1]), GSCMetric.date >= since)
        .group_by(GSCMetric.website_id)
    ).all()
    search_by_site = {
        row[0]: {"clicks": int(row[1] or 0), "impressions": int(row[2] or 0)}
        for row in search_rows
    }

    # The most recent crawl run per website.
    active_runs = {
        row[0]: {"id": row[1], "status": row[2], "progress": row[3], "stage": row[4]}
        for row in db.execute(
            select(
                CrawlRun.website_id, CrawlRun.id, CrawlRun.status,
                CrawlRun.progress_percent, CrawlRun.stage,
            )
            .where(
                CrawlRun.website_id.in_(website_ids or [-1]),
                CrawlRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            )
            .order_by(CrawlRun.id.desc())
        )
    }

    items = []
    for website in websites:
        issues = issues_by_site.get(website.id, {})
        bands = bands_by_site.get(website.id, {})
        items.append(
            {
                "id": website.id,
                "name": website.name,
                "url": website.url,
                "domain": website.domain,
                "is_active": website.is_active,
                "total_pages": website.total_pages,
                "average_seo_score": website.average_seo_score,
                "critical_issues": issues.get(Severity.CRITICAL, 0),
                "high_issues": issues.get(Severity.HIGH, 0),
                "total_issues": sum(issues.values()),
                "p0_pages": bands.get("P0", 0),
                "p1_pages": bands.get("P1", 0),
                "high_priority_pages": bands.get("P0", 0) + bands.get("P1", 0),
                "last_crawled_at": website.last_crawled_at,
                "last_synced_at": website.last_synced_at,
                "last_scored_at": website.last_scored_at,
                "integrations": integrations.get(website.id, {}),
                "traffic": traffic_by_site.get(
                    website.id,
                    {"status": "not_connected", "users": None, "sessions": None, "conversions": None, "revenue": None},
                ),
                "search": search_by_site.get(website.id, {"clicks": 0, "impressions": 0}),
                "active_crawl": active_runs.get(website.id),
                "github_repo": website.github_repo,
            }
        )

    # Most urgent work first — that is the question the screen answers.
    items.sort(key=lambda w: (-w["p0_pages"], -w["critical_issues"], w["name"].lower()))

    totals = {
        "websites": len(items),
        "pages": sum(i["total_pages"] for i in items),
        "critical_issues": sum(i["critical_issues"] for i in items),
        "high_priority_pages": sum(i["high_priority_pages"] for i in items),
        "total_issues": sum(i["total_issues"] for i in items),
        "users": sum(i["traffic"]["users"] for i in items if i["traffic"]["users"] is not None),
        "conversions": sum(i["traffic"]["conversions"] for i in items if i["traffic"]["conversions"] is not None),
        "revenue": sum(i["traffic"]["revenue"] for i in items if i["traffic"]["revenue"] is not None),
        "clicks": sum(i["search"]["clicks"] for i in items),
        "impressions": sum(i["search"]["impressions"] for i in items),
    }

    scored = [i["average_seo_score"] for i in items if i["average_seo_score"] is not None]
    totals["average_seo_score"] = round(sum(scored) / len(scored), 1) if scored else None

    return {"totals": totals, "window_days": window, "websites": items}


@router.get("/websites/{website_id}")
def website_overview(
    website: ReadableWebsite, db: DbSession, window_days: int | None = Query(None)
):
    """Everything the website screen shows above the priority table."""
    window = window_days or settings.priority_metric_window_days
    since = window_start(window)

    active = Page.is_active.is_(True)

    seo_bands = dict(
        db.execute(
            select(Page.seo_category, func.count(Page.id))
            .where(Page.website_id == website.id, active)
            .group_by(Page.seo_category)
        ).all()
    )
    priority_bands = dict(
        db.execute(
            select(Page.priority_band, func.count(Page.id))
            .where(Page.website_id == website.id, active)
            .group_by(Page.priority_band)
        ).all()
    )
    ai_statuses = dict(
        db.execute(
            select(Page.ai_status, func.count(Page.id))
            .where(Page.website_id == website.id, active)
            .group_by(Page.ai_status)
        ).all()
    )
    status_case = case(
        (Page.status_code.between(200, 299), "2xx"),
        (Page.status_code.between(300, 399), "3xx"),
        (Page.status_code.between(400, 499), "4xx"),
        (Page.status_code >= 500, "5xx"),
        else_="unreachable",
    )
    status_codes = dict(
        db.execute(
            select(status_case, func.count(Page.id))
            .where(Page.website_id == website.id, active)
            .group_by(status_case)
        ).all()
    )

    issue_rows = db.execute(
        select(SEOIssue.severity, func.count(SEOIssue.id))
        .join(Page, SEOIssue.page_id == Page.id)
        .where(Page.website_id == website.id, active, SEOIssue.is_resolved.is_(False))
        .group_by(SEOIssue.severity)
    ).all()
    issues_by_severity = {severity: count for severity, count in issue_rows}

    # Grouped by rule alone. A rule can fire at different severities depending on the page (alt
    # text is HIGH when most images lack it, MEDIUM when a few do), and splitting those into two
    # rows would show the same problem twice with partial counts.
    rule_totals: dict[str, dict[str, Any]] = {}
    for rule_id, title, severity, pages in db.execute(
        select(
            SEOIssue.rule_id,
            SEOIssue.title,
            SEOIssue.severity,
            func.count(func.distinct(SEOIssue.page_id)),
        )
        .join(Page, SEOIssue.page_id == Page.id)
        .where(Page.website_id == website.id, active, SEOIssue.is_resolved.is_(False))
        .group_by(SEOIssue.rule_id, SEOIssue.title, SEOIssue.severity)
    ).all():
        entry = rule_totals.setdefault(
            rule_id, {"rule_id": rule_id, "title": title, "severity": severity, "page_count": 0}
        )
        entry["page_count"] += pages
        if severity_rank(severity) > severity_rank(entry["severity"]):
            entry["severity"] = severity

    top_rules = list(rule_totals.values())
    top_rules.sort(key=lambda r: (-severity_rank(r["severity"]), -r["page_count"]))

    ga4_integ = db.scalar(
        select(Integration).where(
            Integration.website_id == website.id, Integration.provider == IntegrationProvider.GA4
        )
    )
    ga4_status = ga4_integ.status if ga4_integ else "not_connected"
    ga4_error = ga4_integ.last_error if ga4_integ else None

    raw_traffic = db.execute(
        select(
            func.sum(GA4Metric.users),
            func.sum(GA4Metric.sessions),
            func.sum(GA4Metric.conversions),
            func.sum(GA4Metric.revenue),
        ).where(GA4Metric.website_id == website.id, GA4Metric.date >= since)
    ).one()

    if ga4_status == "error":
        traffic_payload = {
            "status": "error",
            "has_data": False,
            "users": None,
            "sessions": None,
            "conversions": None,
            "revenue": None,
            "error": ga4_error,
        }
    elif ga4_status == "not_connected":
        traffic_payload = {
            "status": "not_connected",
            "has_data": False,
            "users": None,
            "sessions": None,
            "conversions": None,
            "revenue": None,
            "error": None,
        }
    elif raw_traffic[0] is not None or raw_traffic[1] is not None:
        traffic_payload = {
            "status": "connected",
            "has_data": True,
            "users": int(raw_traffic[0] or 0),
            "sessions": int(raw_traffic[1] or 0),
            "conversions": float(raw_traffic[2] or 0.0),
            "revenue": float(raw_traffic[3] or 0.0),
            "error": None,
        }
    else:
        traffic_payload = {
            "status": "connected",
            "has_data": False,
            "users": 0,
            "sessions": 0,
            "conversions": 0.0,
            "revenue": 0.0,
            "error": None,
        }

    search = db.execute(
        select(
            func.coalesce(func.sum(GSCMetric.clicks), 0),
            func.coalesce(func.sum(GSCMetric.impressions), 0),
            func.avg(GSCMetric.position),
        ).where(GSCMetric.website_id == website.id, GSCMetric.date >= since)
    ).one()

    latest_runs = db.scalars(
        select(CrawlRun).where(CrawlRun.website_id == website.id)
        .order_by(CrawlRun.id.desc()).limit(5)
    ).all()

    integrations = [
        {
            "provider": provider,
            "status": (row.status if row else "not_connected"),
            "account_label": row.account_label if row else None,
            "last_sync_at": row.last_sync_at if row else None,
            "last_error": row.last_error if row else None,
        }
        for provider, row in (
            (
                p,
                db.scalar(
                    select(Integration).where(
                        Integration.website_id == website.id, Integration.provider == p
                    )
                ),
            )
            for p in ALL_PROVIDERS
        )
    ]

    recommendation_count = db.scalar(
        select(func.count(AIRecommendation.id)).where(
            AIRecommendation.website_id == website.id, AIRecommendation.status == "completed"
        )
    )

    return {
        "website": {
            "id": website.id,
            "name": website.name,
            "url": website.url,
            "domain": website.domain,
            "is_active": website.is_active,
            "github_repo": website.github_repo,
            "github_branch": website.github_branch,
            "render_mode": website.render_mode,
            "max_pages": website.max_pages,
        },
        "window_days": window,
        "summary": {
            "total_pages": website.total_pages,
            "average_seo_score": website.average_seo_score,
            "critical_issues": issues_by_severity.get(Severity.CRITICAL, 0),
            "high_issues": issues_by_severity.get(Severity.HIGH, 0),
            "total_issues": sum(issues_by_severity.values()),
            "high_priority_pages": priority_bands.get("P0", 0) + priority_bands.get("P1", 0),
            "ai_recommendations": recommendation_count or 0,
            "last_crawled_at": website.last_crawled_at,
            "last_synced_at": website.last_synced_at,
            "last_scored_at": website.last_scored_at,
        },
        "distribution": {
            "seo_category": {k: v for k, v in seo_bands.items() if k},
            "priority_band": {k: v for k, v in priority_bands.items() if k},
            "ai_status": {k: v for k, v in ai_statuses.items() if k},
            "status_code": status_codes,
            "issues_by_severity": issues_by_severity,
        },
        "top_issues": top_rules[:10],
        "traffic": traffic_payload,
        "search": {
            "clicks": int(search[0]),
            "impressions": int(search[1]),
            "average_position": round(float(search[2]), 1) if search[2] is not None else None,
            "ctr": round(int(search[0]) / int(search[1]), 4) if int(search[1]) else None,
        },
        "integrations": integrations,
        "data_sources": sorted(available_data_sources(db, website.id)),
        "recent_crawls": [
            {
                "id": run.id,
                "status": run.status,
                "trigger": run.trigger,
                "mode": run.mode,
                "stage": run.stage,
                "progress_percent": run.progress_percent,
                "pages_crawled": run.pages_crawled,
                "pages_analysed": run.pages_analysed,
                "average_seo_score": run.average_seo_score,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "duration_seconds": run.duration_seconds,
                "error": run.error,
            }
            for run in latest_runs
        ],
    }


@router.get("/websites/{website_id}/trends")
def website_trends(
    website: ReadableWebsite, db: DbSession, days: int = Query(90, ge=7, le=365)
):
    """Daily series for the website charts, merging provider data with our own rollups."""
    since = date.today() - timedelta(days=days)
    series: dict[date, dict[str, Any]] = {}

    def slot(day: date) -> dict[str, Any]:
        return series.setdefault(
            day,
            {
                "date": day.isoformat(),
                "clicks": 0, "impressions": 0, "users": 0, "sessions": 0,
                "conversions": 0.0, "revenue": 0.0,
                "seo_score": None, "issue_count": 0, "critical_count": 0,
            },
        )

    for day, clicks, impressions in db.execute(
        select(
            GSCMetric.date, func.sum(GSCMetric.clicks), func.sum(GSCMetric.impressions)
        )
        .where(GSCMetric.website_id == website.id, GSCMetric.date >= since)
        .group_by(GSCMetric.date)
    ):
        entry = slot(day)
        entry["clicks"] = int(clicks or 0)
        entry["impressions"] = int(impressions or 0)

    for day, users, sessions, conversions, revenue in db.execute(
        select(
            GA4Metric.date, func.sum(GA4Metric.users), func.sum(GA4Metric.sessions),
            func.sum(GA4Metric.conversions), func.sum(GA4Metric.revenue),
        )
        .where(GA4Metric.website_id == website.id, GA4Metric.date >= since)
        .group_by(GA4Metric.date)
    ):
        entry = slot(day)
        entry["users"] = int(users or 0)
        entry["sessions"] = int(sessions or 0)
        entry["conversions"] = float(conversions or 0)
        entry["revenue"] = float(revenue or 0)

    for row in db.scalars(
        select(HistoricalMetric).where(
            HistoricalMetric.website_id == website.id,
            HistoricalMetric.scope == "website",
            HistoricalMetric.date >= since,
        )
    ):
        entry = slot(row.date)
        entry["seo_score"] = row.seo_score
        entry["issue_count"] = row.issue_count
        entry["critical_count"] = row.critical_count

    return {"days": days, "points": [series[day] for day in sorted(series)]}
