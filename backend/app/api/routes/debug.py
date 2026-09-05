"""SEO Audit Page Debug & GA4 Debug Endpoints.

Provides detailed page-level evidence and raw GA4 API inspection payload for URL-by-URL verification against external auditors (e.g., JetOctopus).
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from ...core.deps import DbSession, ReadableWebsite
from ...models import Integration, IntegrationProvider, Page, SEOAudit, SEOIssue
from ...services.integrations import ga4
from ...services.integrations.google_oauth import get_access_token

router = APIRouter(prefix="/api", tags=["debug"])


@router.get("/websites/{website_id}/pages/{page_id}/debug")
def debug_page_seo(
    website: ReadableWebsite,
    page_id: int,
    db: DbSession,
) -> dict[str, Any]:
    """Inspect complete raw SEO evidence, signals, and detected issues for one page."""
    page = db.scalar(
        select(Page).where(Page.id == page_id, Page.website_id == website.id)
    )
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Page {page_id} not found on website {website.id}.",
        )

    audit = db.scalar(
        select(SEOAudit)
        .where(SEOAudit.page_id == page.id)
        .order_by(SEOAudit.id.desc())
    )

    issues = db.scalars(
        select(SEOIssue)
        .where(SEOIssue.page_id == page.id)
        .order_by(SEOIssue.id.asc())
    ).all() if audit else []

    title = (page.title or "").strip()
    desc = (page.meta_description or "").strip()

    return {
        "page_id": page.id,
        "website_id": page.website_id,
        "url": page.url,
        "final_url": page.final_url or page.url,
        "status_code": page.status_code,
        "content_type": page.content_type,
        "redirect_chain": page.redirect_chain or [],
        "was_rendered": page.was_rendered,
        "render_error": getattr(page, "render_error", None),
        "response_time_ms": page.response_time_ms,
        "freshness": {
            "last_crawled_at": page.last_crawled_at.isoformat() if page.last_crawled_at else None,
            "content_captured_at": (
                page.content_captured_at.isoformat()
                if getattr(page, "content_captured_at", None) else None
            ),
            # When the content is older than the crawl, the most recent crawl retrieved no
            # document and the previous signals were kept rather than blanked out.
            "content_is_from_an_earlier_crawl": bool(
                page.last_crawled_at
                and getattr(page, "content_captured_at", None)
                and page.content_captured_at < page.last_crawled_at
            ),
        },
        "signals": {
            "title": title or None,
            "title_length": len(title),
            "meta_description": desc or None,
            "meta_description_length": len(desc),
            "canonical_url": page.canonical_url,
            "canonical_raw": getattr(page, "canonical_raw", None),
            "canonical_count": getattr(page, "canonical_count", 0),
            "canonical_status": getattr(page, "canonical_status", None),
            "robots_meta": page.robots_directive,
            "x_robots_tag": page.x_robots_tag,
            "h1": page.h1,
            "h1_count": page.h1_count,
            "h2_count": page.h2_count,
            "h3_count": page.h3_count,
            "h4_count": getattr(page, "h4_count", 0),
            "h5_count": getattr(page, "h5_count", 0),
            "h6_count": getattr(page, "h6_count", 0),
            "empty_heading_count": getattr(page, "empty_heading_count", 0),
            "title_count": getattr(page, "title_count", 0),
            "meta_description_count": getattr(page, "meta_description_count", 0),
            "meta_robots_count": getattr(page, "meta_robots_count", 0),
            # All three word measurements, so a disagreement with another tool can be explained
            # by methodology rather than argued about.
            "word_count": page.word_count,
            "raw_word_count": getattr(page, "raw_word_count", 0),
            "visible_word_count": getattr(page, "visible_word_count", 0),
            "main_content_word_count": getattr(page, "main_content_word_count", 0),
            "content_scope": getattr(page, "content_scope", None),
            "internal_link_count": page.internal_link_count,
            "external_link_count": page.external_link_count,
            "inbound_internal_links": page.inbound_internal_links,
            "sponsored_link_count": getattr(page, "sponsored_link_count", 0),
            "ugc_link_count": getattr(page, "ugc_link_count", 0),
            "image_count": page.image_count,
            "missing_alt_count": page.missing_alt_count,
            "empty_alt_count": getattr(page, "empty_alt_count", 0),
            "tracking_pixel_count": getattr(page, "tracking_pixel_count", 0),
            "non_http_link_count": getattr(page, "non_http_link_count", 0),
            "pagination_next": getattr(page, "pagination_next", None),
            "pagination_prev": getattr(page, "pagination_prev", None),
            "has_viewport": page.has_viewport,
            "has_structured_data": page.has_structured_data,
            "structured_data_types": page.structured_data_types or [],
            "structured_data_formats": getattr(page, "structured_data_formats", None) or [],
            "has_open_graph": page.has_open_graph,
            "crawl_quality": getattr(page, "crawl_quality", "ok"),
            "extraction_errors": getattr(page, "extraction_errors", None) or [],
        },
        "audit_summary": {
            "audit_id": audit.id if audit else None,
            "seo_score": audit.seo_score if audit else None,
            "category": audit.category if audit else None,
            "highest_severity": audit.highest_severity if audit else None,
            "issue_count": len(issues),
            "audited_at": audit.audited_at.isoformat() if audit and audit.audited_at else None,
        },
        "issues": [
            {
                "id": issue.id,
                "rule_id": issue.rule_id,
                "check_type": issue.check_type,
                "category": issue.category,
                "severity": issue.severity,
                "title": issue.title,
                "description": issue.description,
                "evidence": issue.evidence,
            }
            for issue in issues
        ],
    }


@router.get("/integrations/ga4/debug")
async def debug_ga4_integration(
    website: ReadableWebsite,
    db: DbSession,
) -> dict[str, Any]:
    """Inspect raw GA4 API query details, property ID validation, and metrics for a website."""
    integration = db.scalar(
        select(Integration).where(
            Integration.website_id == website.id,
            Integration.provider == IntegrationProvider.GA4,
        )
    )

    if integration is None:
        return {
            "website_id": website.id,
            "status": "not_connected",
            "message": "No GA4 integration configured for this website.",
        }

    property_id = str((integration.config or {}).get("property_id") or integration.account_label or "").strip()
    clean_property_id = property_id.removeprefix("properties/")

    token = None
    auth_error = None
    try:
        token = await get_access_token(db, integration)
    except Exception as exc:
        auth_error = str(exc)

    report_result = None
    api_error = None
    if token and clean_property_id.isdigit():
        try:
            report_result = await ga4.sync(db, website, days=90)
        except Exception as exc:
            api_error = str(exc)

    return {
        "website_id": website.id,
        "website_name": website.name,
        "website_url": website.url,
        "integration_status": integration.status,
        "raw_property_id": property_id,
        "clean_property_id": clean_property_id,
        "is_numeric_property_id": clean_property_id.isdigit(),
        "has_access_token": bool(token),
        "auth_error": auth_error,
        "last_sync_error": integration.last_error,
        "ga4_90_day_sync_result": report_result,
        "api_error": api_error,
    }
