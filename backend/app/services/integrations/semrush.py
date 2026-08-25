"""Semrush connector.

Semrush's Analytics API is CSV-over-HTTP with a ``;``-separated body and an API key in the query
string. Two report types are used:

  * ``url_organic``    — the keywords one URL ranks for (position, volume, CPC, difficulty)
  * ``domain_organic`` — the domain's top-ranking URLs, used to decide which pages are worth a
                         per-URL call at all

Semrush charges API units per row, so the connector is deliberately budget-aware: it asks the
domain report which pages actually have organic visibility and only spends per-URL calls on the
top ``max_pages`` of those, rather than querying every crawled URL.

"Striking distance" means positions 4-20: already ranking, not yet earning meaningful clicks, and
usually the cheapest wins available — which is exactly the opportunity signal the priority engine
wants.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...core.crypto import mask_secret
from ...core.errors import IntegrationError
from ...models import IntegrationProvider, Page, SemrushMetric, Website
from .base import (
    integration_client,
    mark_sync_failure,
    mark_sync_started,
    mark_sync_success,
    read_credentials,
    request_with_retry,
    require_integration,
)
from .matching import PageResolver

logger = logging.getLogger(__name__)

STRIKING_DISTANCE_MIN = 4
STRIKING_DISTANCE_MAX = 20
MAX_KEYWORDS_STORED = 25
DEFAULT_MAX_PAGES = 250

URL_ORGANIC_COLUMNS = "Ph,Po,Nq,Cp,Co,Kd,Tr,Tc"
DOMAIN_ORGANIC_COLUMNS = "Ur,Pc,Tg,Tr"
BACKLINKS_COLUMNS = "target,backlinks_num,domains_num"


class SemrushError(IntegrationError):
    code = "semrush_error"


def _parse_csv(text: str) -> list[dict[str, str]]:
    """Parse Semrush's ``;``-separated CSV, surfacing its plain-text error responses."""
    body = (text or "").strip()
    if not body:
        return []

    # Semrush signals problems with a bare line such as "ERROR 50 :: NOTHING FOUND".
    if body.upper().startswith("ERROR"):
        if "NOTHING FOUND" in body.upper():
            return []
        raise SemrushError(f"Semrush API error: {body.splitlines()[0][:200]}")

    reader = csv.DictReader(io.StringIO(body), delimiter=";")
    return [row for row in reader if row]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    return int(_to_float(value, default))


async def _call(api_key: str, params: dict[str, Any]) -> list[dict[str, str]]:
    """Issue one Semrush report request."""
    query = {**params, "key": api_key}
    async with integration_client() as client:
        response = await request_with_retry(
            client, "GET", f"{settings.semrush_api_base}/", provider="Semrush", params=query
        )
    return _parse_csv(response.text)


async def verify_api_key(api_key: str) -> dict[str, Any]:
    """Confirm a key works and report the remaining API unit balance."""
    async with integration_client(timeout=20) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://www.semrush.com/users/countapiunits.html",
            provider="Semrush",
            params={"key": api_key},
            max_retries=2,
        )
    balance = response.text.strip()
    if not balance.isdigit():
        raise SemrushError("Semrush rejected the API key.")
    return {"api_units_remaining": int(balance), "key_hint": mask_secret(api_key)}


async def fetch_domain_organic_pages(
    api_key: str, domain: str, database: str, limit: int
) -> list[dict[str, Any]]:
    """The domain's ranking URLs, ordered by estimated organic traffic."""
    rows = await _call(
        api_key,
        {
            "type": "domain_organic_unique",
            "domain": domain,
            "database": database,
            "display_limit": limit,
            "export_columns": DOMAIN_ORGANIC_COLUMNS,
        },
    )
    return [
        {
            "url": row.get("Url") or row.get("Ur") or "",
            "keyword_count": _to_int(row.get("Number of Keywords") or row.get("Pc")),
            "traffic": _to_int(row.get("Traffic") or row.get("Tg") or row.get("Tr")),
        }
        for row in rows
        if (row.get("Url") or row.get("Ur"))
    ]


async def fetch_url_keywords(
    api_key: str, url: str, database: str, limit: int = 100
) -> list[dict[str, Any]]:
    """Keywords one URL ranks for."""
    rows = await _call(
        api_key,
        {
            "type": "url_organic",
            "url": url,
            "database": database,
            "display_limit": limit,
            "export_columns": URL_ORGANIC_COLUMNS,
        },
    )
    keywords = []
    for row in rows:
        keyword = row.get("Keyword") or row.get("Ph")
        if not keyword:
            continue
        keywords.append(
            {
                "keyword": keyword,
                "position": _to_int(row.get("Position") or row.get("Po")),
                "volume": _to_int(row.get("Search Volume") or row.get("Nq")),
                "cpc": _to_float(row.get("CPC") or row.get("Cp")),
                "competition": _to_float(row.get("Competition") or row.get("Co")),
                "difficulty": _to_float(row.get("Keyword Difficulty Index") or row.get("Kd")),
                "traffic_percent": _to_float(row.get("Traffic (%)") or row.get("Tr")),
            }
        )
    return keywords


async def fetch_backlinks(api_key: str, target: str) -> dict[str, int]:
    """Backlink and referring-domain totals for a URL or domain."""
    try:
        rows = await _call(
            api_key,
            {
                "type": "backlinks_overview",
                "target": target,
                "target_type": "url",
                "export_columns": BACKLINKS_COLUMNS,
            },
        )
    except SemrushError:
        # Backlink reports are a separate subscription; absence must not fail the whole sync.
        return {"backlinks": 0, "referring_domains": 0}

    if not rows:
        return {"backlinks": 0, "referring_domains": 0}
    row = rows[0]
    return {
        "backlinks": _to_int(row.get("backlinks_num") or row.get("Backlinks")),
        "referring_domains": _to_int(row.get("domains_num") or row.get("Referring Domains")),
    }


def summarise_keywords(keywords: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a keyword list to the opportunity signals the priority engine consumes."""
    if not keywords:
        return {
            "organic_keywords": 0,
            "striking_distance_keywords": 0,
            "opportunity_volume": 0,
            "best_position": None,
            "average_position": None,
            "organic_traffic": 0,
        }

    positions = [k["position"] for k in keywords if k["position"] > 0]
    striking = [
        k for k in keywords if STRIKING_DISTANCE_MIN <= k["position"] <= STRIKING_DISTANCE_MAX
    ]

    return {
        "organic_keywords": len(keywords),
        "striking_distance_keywords": len(striking),
        "opportunity_volume": sum(k["volume"] for k in striking),
        "best_position": min(positions) if positions else None,
        "average_position": round(sum(positions) / len(positions), 1) if positions else None,
        "organic_traffic": 0,
    }


async def sync(
    db: Session,
    website: Website,
    *,
    max_pages: int | None = None,
    include_backlinks: bool = True,
    today: date | None = None,
) -> dict[str, Any]:
    """Pull Semrush data for a website's highest-visibility pages."""
    integration = require_integration(db, website.id, IntegrationProvider.SEMRUSH)
    credentials = read_credentials(integration)
    api_key = credentials.get("api_key")
    if not api_key:
        raise SemrushError("No Semrush API key is stored for this website.")

    config = integration.config or {}
    database = config.get("database") or settings.semrush_database
    limit = max_pages or config.get("max_pages") or DEFAULT_MAX_PAGES
    snapshot_date = today or date.today()

    mark_sync_started(db, integration)
    try:
        resolver = PageResolver.build(db, website.id, website.url)
        ranking_pages = await fetch_domain_organic_pages(
            api_key, website.domain, database, limit
        )

        if not ranking_pages:
            # The domain has no organic visibility Semrush knows about. That is a valid result,
            # not an error — record the sync and move on.
            db.commit()
            mark_sync_success(db, integration, "No organic ranking URLs reported.")
            return {
                "provider": "semrush",
                "database": database,
                "ranking_pages": 0,
                "metrics_upserted": 0,
                **resolver.summary,
            }

        upserted = 0
        for entry in ranking_pages[:limit]:
            page_id = resolver.resolve(entry["url"])
            if page_id is None:
                continue

            keywords = await fetch_url_keywords(api_key, entry["url"], database)
            summary = summarise_keywords(keywords)
            summary["organic_traffic"] = entry["traffic"]

            backlinks = (
                await fetch_backlinks(api_key, entry["url"])
                if include_backlinks
                else {"backlinks": 0, "referring_domains": 0}
            )

            metric = db.scalar(
                select(SemrushMetric).where(
                    SemrushMetric.page_id == page_id, SemrushMetric.date == snapshot_date
                )
            )
            if metric is None:
                metric = SemrushMetric(
                    website_id=website.id, page_id=page_id, date=snapshot_date
                )
                db.add(metric)

            metric.database = database
            metric.organic_keywords = summary["organic_keywords"] or entry["keyword_count"]
            metric.organic_traffic = summary["organic_traffic"]
            metric.organic_cost = round(
                sum(k["volume"] * k["cpc"] for k in keywords) / 100, 2
            )
            metric.striking_distance_keywords = summary["striking_distance_keywords"]
            metric.opportunity_volume = summary["opportunity_volume"]
            metric.best_position = summary["best_position"]
            metric.average_position = summary["average_position"]
            metric.backlinks = backlinks["backlinks"]
            metric.referring_domains = backlinks["referring_domains"]
            metric.keywords = sorted(
                keywords, key=lambda k: (-k["volume"], k["position"])
            )[:MAX_KEYWORDS_STORED]

            upserted += 1

        db.flush()
        db.commit()

        result = {
            "provider": "semrush",
            "database": database,
            "ranking_pages": len(ranking_pages),
            "metrics_upserted": upserted,
            "date": snapshot_date.isoformat(),
            **resolver.summary,
        }
        mark_sync_success(db, integration, f"{upserted} pages refreshed from Semrush.")
        logger.info("Semrush sync for website %s: %s", website.id, result)
        return result

    except Exception as exc:
        db.rollback()
        mark_sync_failure(db, integration, f"{type(exc).__name__}: {exc}")
        raise


def keyword_opportunities(db: Session, website_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Striking-distance keywords across the site, ranked by the traffic they could unlock."""
    rows = db.execute(
        select(SemrushMetric.page_id, Page.url, SemrushMetric.keywords, SemrushMetric.date)
        .join(Page, SemrushMetric.page_id == Page.id)
        .where(SemrushMetric.website_id == website_id)
        .order_by(SemrushMetric.date.desc())
    ).all()

    seen_pages: set[int] = set()
    opportunities: list[dict[str, Any]] = []
    for page_id, url, keywords, _ in rows:
        if page_id in seen_pages:
            continue  # only the newest snapshot per page
        seen_pages.add(page_id)
        for keyword in keywords or []:
            position = keyword.get("position", 0)
            if STRIKING_DISTANCE_MIN <= position <= STRIKING_DISTANCE_MAX:
                opportunities.append(
                    {
                        "page_id": page_id,
                        "url": url,
                        "keyword": keyword.get("keyword"),
                        "position": position,
                        "volume": keyword.get("volume", 0),
                        "difficulty": keyword.get("difficulty"),
                        "cpc": keyword.get("cpc"),
                    }
                )

    opportunities.sort(key=lambda o: (-o["volume"], o["position"]))
    return opportunities[:limit]
