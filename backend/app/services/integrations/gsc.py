"""Google Search Console connector.

Uses the Search Analytics API directly over HTTPS rather than the generated Google client library:
the surface needed is two endpoints, and going direct keeps the dependency tree small and the
retry/backoff behaviour under our own control.

Two queries per sync:
  * ``dimensions=[date, page]``  → the daily per-page time series that is stored
  * ``dimensions=[page, query]`` → top queries, attached to each page's newest day with data
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...core.errors import IntegrationError
from ...models import GSCMetric, Integration, IntegrationProvider, Website
from .base import (
    integration_client,
    mark_sync_failure,
    mark_sync_started,
    mark_sync_success,
    request_with_retry,
    require_integration,
)
from .google_oauth import get_access_token
from .matching import PageResolver, site_url_variants

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/webmasters/v3"
#: Search Console caps a single response at 25 000 rows.
ROW_LIMIT = 25_000
MAX_QUERIES_PER_PAGE = 10
#: Search Console data lags roughly two to three days.
DATA_LAG_DAYS = 3


async def list_sites(access_token: str) -> list[dict[str, Any]]:
    """Every property the authorised account can read."""
    async with integration_client() as client:
        response = await request_with_retry(
            client,
            "GET",
            f"{API_BASE}/sites",
            provider="Search Console",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    return response.json().get("siteEntry", [])


async def detect_site_url(access_token: str, website: Website) -> str | None:
    """Pick the Search Console property that corresponds to a website URL."""
    entries = await list_sites(access_token)
    available = {entry.get("siteUrl", "") for entry in entries}
    for candidate in site_url_variants(website.url):
        if candidate in available:
            return candidate
    return None


async def query_search_analytics(
    access_token: str,
    site_url: str,
    *,
    start_date: date,
    end_date: date,
    dimensions: list[str],
    row_limit: int = ROW_LIMIT,
    start_row: int = 0,
) -> list[dict[str, Any]]:
    """One Search Analytics page of results."""
    from urllib.parse import quote

    body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "dimensions": dimensions,
        "rowLimit": row_limit,
        "startRow": start_row,
        "type": "web",
        "dataState": "final",
    }
    async with integration_client() as client:
        response = await request_with_retry(
            client,
            "POST",
            f"{API_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query",
            provider="Search Console",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    return response.json().get("rows", [])


async def fetch_all_rows(
    access_token: str,
    site_url: str,
    *,
    start_date: date,
    end_date: date,
    dimensions: list[str],
    max_rows: int = 100_000,
) -> list[dict[str, Any]]:
    """Page through Search Analytics until the API stops returning rows."""
    rows: list[dict[str, Any]] = []
    start_row = 0
    while len(rows) < max_rows:
        batch = await query_search_analytics(
            access_token,
            site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            row_limit=min(ROW_LIMIT, max_rows - len(rows)),
            start_row=start_row,
        )
        rows.extend(batch)
        if len(batch) < ROW_LIMIT:
            break
        start_row += len(batch)
    return rows


def _upsert_metrics(
    db: Session,
    website_id: int,
    resolver: PageResolver,
    rows: list[dict[str, Any]],
) -> int:
    """Insert or update ``gsc_metrics`` from ``[date, page]`` rows."""
    if not rows:
        return 0

    parsed: dict[tuple[int, date], dict[str, Any]] = {}
    for row in rows:
        keys = row.get("keys") or []
        if len(keys) < 2:
            continue
        try:
            day = date.fromisoformat(keys[0])
        except ValueError:
            continue
        page_id = resolver.resolve(keys[1])
        if page_id is None:
            continue

        # Several source URLs can collapse onto one page (protocol/host variants); sum them.
        entry = parsed.setdefault(
            (page_id, day), {"clicks": 0, "impressions": 0, "position_weight": 0.0}
        )
        clicks = int(row.get("clicks", 0) or 0)
        impressions = int(row.get("impressions", 0) or 0)
        entry["clicks"] += clicks
        entry["impressions"] += impressions
        # Weight position by impressions so the merged average reflects real visibility.
        entry["position_weight"] += float(row.get("position", 0) or 0) * impressions

    if not parsed:
        return 0

    page_ids = {page_id for page_id, _ in parsed}
    days = {day for _, day in parsed}
    existing = {
        (metric.page_id, metric.date): metric
        for metric in db.scalars(
            select(GSCMetric).where(
                GSCMetric.website_id == website_id,
                GSCMetric.page_id.in_(page_ids),
                GSCMetric.date.in_(days),
            )
        )
    }

    for (page_id, day), values in parsed.items():
        impressions = values["impressions"]
        metric = existing.get((page_id, day))
        if metric is None:
            metric = GSCMetric(website_id=website_id, page_id=page_id, date=day)
            db.add(metric)
        metric.clicks = values["clicks"]
        metric.impressions = impressions
        metric.ctr = round(values["clicks"] / impressions, 6) if impressions else 0.0
        metric.position = (
            round(values["position_weight"] / impressions, 2) if impressions else None
        )

    db.flush()
    return len(parsed)


def _attach_queries(
    db: Session,
    website_id: int,
    resolver: PageResolver,
    rows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> int:
    """Store each page's top queries on its most recent metric row in the window.

    Queries are reported for the window as a whole, not per day. They are attached to the newest
    day the page actually has data for — creating a row on ``end_date`` purely to hold them would
    inject a zero-click day into the time series and drag every average down.
    """
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        keys = row.get("keys") or []
        if len(keys) < 2:
            continue
        page_id = resolver.resolve(keys[0])
        if page_id is None:
            continue
        grouped[page_id].append(
            {
                "query": keys[1],
                "clicks": int(row.get("clicks", 0) or 0),
                "impressions": int(row.get("impressions", 0) or 0),
                "ctr": round(float(row.get("ctr", 0) or 0), 6),
                "position": round(float(row.get("position", 0) or 0), 2),
            }
        )

    if not grouped:
        return 0

    # Keep only the newest metric row per page within the window.
    latest: dict[int, GSCMetric] = {}
    for metric in db.scalars(
        select(GSCMetric)
        .where(
            GSCMetric.website_id == website_id,
            GSCMetric.page_id.in_(grouped.keys()),
            GSCMetric.date >= start_date,
            GSCMetric.date <= end_date,
        )
        .order_by(GSCMetric.date.asc())
    ):
        latest[metric.page_id] = metric

    attached = 0
    for page_id, queries in grouped.items():
        metric = latest.get(page_id)
        if metric is None:
            # The page has queries but no daily rows — nothing to attach them to.
            continue
        queries.sort(key=lambda q: (-q["clicks"], -q["impressions"]))
        metric.queries = queries[:MAX_QUERIES_PER_PAGE]
        attached += 1

    db.flush()
    return attached


async def sync(
    db: Session,
    website: Website,
    *,
    days: int | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Pull Search Console data for a website and upsert it into ``gsc_metrics``."""
    integration = require_integration(db, website.id, IntegrationProvider.GSC)
    site_url = (integration.config or {}).get("site_url")
    if not site_url:
        raise IntegrationError(
            "No Search Console property is selected for this website.",
            code="integration_not_configured",
        )

    mark_sync_started(db, integration)
    try:
        access_token = await get_access_token(db, integration)

        window = days or settings.integration_sync_window_days
        end_date = end or (date.today() - timedelta(days=DATA_LAG_DAYS))
        start_date = end_date - timedelta(days=window - 1)

        resolver = PageResolver.build(db, website.id, website.url)

        daily_rows = await fetch_all_rows(
            access_token,
            site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=["date", "page"],
        )
        metric_count = _upsert_metrics(db, website.id, resolver, daily_rows)

        query_rows = await fetch_all_rows(
            access_token,
            site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=["page", "query"],
            max_rows=ROW_LIMIT,
        )
        query_count = _attach_queries(
            db, website.id, resolver, query_rows, start_date, end_date
        )

        db.commit()

        summary = {
            "provider": "gsc",
            "site_url": site_url,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "rows_fetched": len(daily_rows),
            "metrics_upserted": metric_count,
            "pages_with_queries": query_count,
            **resolver.summary,
        }
        mark_sync_success(
            db, integration, f"{metric_count} page/day rows from {start_date} to {end_date}"
        )
        logger.info("Search Console sync for website %s: %s", website.id, summary)
        return summary

    except Exception as exc:
        db.rollback()
        mark_sync_failure(db, integration, f"{type(exc).__name__}: {exc}")
        raise


async def backfill(db: Session, website: Website, days: int | None = None) -> dict[str, Any]:
    """Initial historical load, run once when the integration is first connected."""
    return await sync(db, website, days=days or settings.integration_sync_backfill_days)
