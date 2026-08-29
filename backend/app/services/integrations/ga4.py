"""Google Analytics 4 connector.

Uses the GA4 Data API (``runReport``) over the same OAuth grant as Search Console. The report is
requested with ``date`` × ``pagePath`` so the result is directly comparable with Search Console's
per-page daily series.

Revenue is read from ``purchaseRevenue`` where the property records ecommerce, falling back to
``totalRevenue`` otherwise — GA4 properties without ecommerce return zero for the former, which
would silently erase the business signal the priority engine depends on.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...core.errors import IntegrationError
from ...models import GA4Metric, IntegrationProvider, Website
from .base import (
    integration_client,
    mark_sync_failure,
    mark_sync_started,
    mark_sync_success,
    request_with_retry,
    require_integration,
)
from .google_oauth import get_access_token
from .matching import PageResolver

logger = logging.getLogger(__name__)

DATA_API = "https://analyticsdata.googleapis.com/v1beta"
ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"
ROW_LIMIT = 100_000

METRICS = [
    "totalUsers",
    "newUsers",
    "sessions",
    "screenPageViews",
    "engagedSessions",
    "engagementRate",
    "userEngagementDuration",
    "bounceRate",
    "conversions",
    "purchaseRevenue",
]
DIMENSIONS = ["date", "pagePath"]


async def list_properties(access_token: str) -> list[dict[str, Any]]:
    """GA4 properties visible to the authorised account, across all its accounts."""
    properties: list[dict[str, Any]] = []
    async with integration_client() as client:
        summaries = await request_with_retry(
            client,
            "GET",
            f"{ADMIN_API}/accountSummaries?pageSize=200",
            provider="Google Analytics",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        for account in summaries.json().get("accountSummaries", []):
            for prop in account.get("propertySummaries", []):
                properties.append(
                    {
                        # "properties/123456789" -> "123456789"
                        "property_id": prop.get("property", "").split("/")[-1],
                        "display_name": prop.get("displayName"),
                        "account": account.get("displayName"),
                    }
                )
    return properties


async def run_report(
    access_token: str,
    property_id: str,
    *,
    start_date: date,
    end_date: date,
    offset: int = 0,
    limit: int = ROW_LIMIT,
) -> dict[str, Any]:
    """One GA4 ``runReport`` call."""
    clean_property_id = property_id.strip().removeprefix("properties/")
    body = {
        "dateRanges": [
            {"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}
        ],
        "dimensions": [{"name": name} for name in DIMENSIONS],
        "metrics": [{"name": name} for name in METRICS],
        "limit": limit,
        "offset": offset,
        "keepEmptyRows": False,
    }
    logger.info(
        "Requesting GA4 report for property %s from %s to %s (offset %d)",
        clean_property_id,
        start_date,
        end_date,
        offset,
    )
    async with integration_client() as client:
        response = await request_with_retry(
            client,
            "POST",
            f"{DATA_API}/properties/{clean_property_id}:runReport",
            provider="Google Analytics",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    return response.json()



def _parse_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn GA4's positional header/row format into named dictionaries."""
    metric_names = [h.get("name") for h in payload.get("metricHeaders", [])]
    dimension_names = [h.get("name") for h in payload.get("dimensionHeaders", [])]

    parsed: list[dict[str, Any]] = []
    for row in payload.get("rows", []):
        record: dict[str, Any] = {}
        for name, value in zip(dimension_names, row.get("dimensionValues", [])):
            record[name] = value.get("value")
        for name, value in zip(metric_names, row.get("metricValues", [])):
            raw = value.get("value", "0")
            try:
                record[name] = float(raw)
            except (TypeError, ValueError):
                record[name] = 0.0
        parsed.append(record)
    return parsed


def _upsert_metrics(
    db: Session,
    website_id: int,
    resolver: PageResolver,
    rows: list[dict[str, Any]],
    currency: str | None,
) -> int:
    if not rows:
        return 0

    aggregated: dict[tuple[int, date], dict[str, float]] = {}
    for row in rows:
        raw_date = row.get("date") or ""
        try:
            # GA4 returns YYYYMMDD.
            day = date(int(raw_date[0:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        except (ValueError, IndexError):
            continue

        page_id = resolver.resolve(row.get("pagePath") or "")
        if page_id is None:
            continue

        entry = aggregated.setdefault(
            (page_id, day),
            {
                "users": 0.0, "new_users": 0.0, "sessions": 0.0, "views": 0.0,
                "engaged": 0.0, "engagement_weight": 0.0, "duration": 0.0,
                "bounce_weight": 0.0, "conversions": 0.0, "revenue": 0.0,
            },
        )
        sessions = row.get("sessions", 0.0)
        entry["users"] += row.get("totalUsers", 0.0)
        entry["new_users"] += row.get("newUsers", 0.0)
        entry["sessions"] += sessions
        entry["views"] += row.get("screenPageViews", 0.0)
        entry["engaged"] += row.get("engagedSessions", 0.0)
        # Rates are per-row averages; weight by sessions before merging duplicate paths.
        entry["engagement_weight"] += row.get("engagementRate", 0.0) * sessions
        entry["bounce_weight"] += row.get("bounceRate", 0.0) * sessions
        entry["duration"] += row.get("userEngagementDuration", 0.0)
        entry["conversions"] += row.get("conversions", 0.0)
        entry["revenue"] += row.get("purchaseRevenue", 0.0) or row.get("totalRevenue", 0.0)

    if not aggregated:
        return 0

    page_ids = {page_id for page_id, _ in aggregated}
    days = {day for _, day in aggregated}
    existing = {
        (metric.page_id, metric.date): metric
        for metric in db.scalars(
            select(GA4Metric).where(
                GA4Metric.website_id == website_id,
                GA4Metric.page_id.in_(page_ids),
                GA4Metric.date.in_(days),
            )
        )
    }

    for (page_id, day), values in aggregated.items():
        metric = existing.get((page_id, day))
        if metric is None:
            metric = GA4Metric(website_id=website_id, page_id=page_id, date=day)
            db.add(metric)

        sessions = values["sessions"]
        metric.users = int(values["users"])
        metric.new_users = int(values["new_users"])
        metric.sessions = int(sessions)
        metric.screen_page_views = int(values["views"])
        metric.engaged_sessions = int(values["engaged"])
        metric.engagement_rate = round(values["engagement_weight"] / sessions, 4) if sessions else 0.0
        metric.bounce_rate = round(values["bounce_weight"] / sessions, 4) if sessions else 0.0
        metric.average_engagement_time = (
            round(values["duration"] / sessions, 2) if sessions else 0.0
        )
        metric.conversions = round(values["conversions"], 2)
        metric.revenue = round(values["revenue"], 2)
        metric.currency = currency

    db.flush()
    return len(aggregated)


async def sync(
    db: Session,
    website: Website,
    *,
    days: int | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Pull GA4 data for a website and upsert it into ``ga4_metrics``."""
    integration = require_integration(db, website.id, IntegrationProvider.GA4)
    property_id = (integration.config or {}).get("property_id") or integration.account_id
    if not property_id:
        raise IntegrationError(
            "No GA4 property is selected for this website.",
            code="integration_not_configured",
        )

    mark_sync_started(db, integration)
    try:
        access_token = await get_access_token(db, integration)

        window = days or settings.integration_sync_backfill_days
        end_date = end or (date.today() - timedelta(days=1))
        start_date = end_date - timedelta(days=window - 1)

        resolver = PageResolver.build(db, website.id, website.url)

        rows: list[dict[str, Any]] = []
        offset = 0
        currency: str | None = None
        while True:
            payload = await run_report(
                access_token,
                property_id,
                start_date=start_date,
                end_date=end_date,
                offset=offset,
            )
            currency = currency or payload.get("metadata", {}).get("currencyCode")
            batch = _parse_rows(payload)
            rows.extend(batch)
            row_count = int(payload.get("rowCount", len(rows)))
            offset += len(batch)
            if not batch or offset >= row_count:
                break

        metric_count = _upsert_metrics(db, website.id, resolver, rows, currency)
        db.commit()

        summary = {
            "provider": "ga4",
            "property_id": property_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "rows_fetched": len(rows),
            "metrics_upserted": metric_count,
            "currency": currency,
            **resolver.summary,
        }
        mark_sync_success(
            db, integration, f"{metric_count} page/day rows from {start_date} to {end_date}"
        )
        logger.info("GA4 sync for website %s: %s", website.id, summary)
        return summary

    except Exception as exc:
        db.rollback()
        mark_sync_failure(db, integration, f"{type(exc).__name__}: {exc}")
        raise


async def backfill(db: Session, website: Website, days: int | None = None) -> dict[str, Any]:
    return await sync(db, website, days=days or settings.integration_sync_backfill_days)
