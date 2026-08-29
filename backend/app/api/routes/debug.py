"""SEO Audit Page Debug & GA4 Debug Endpoints.

Provides detailed page-level evidence and raw GA4 API inspection payload for URL-by-URL verification against external auditors (e.g., JetOctopus).
"""

from __future__ import annotations

import asyncio
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.deps import CurrentUser, DbSession, get_current_user
from ...models import Integration, IntegrationProvider, Page, SEOAudit, SEOIssue, Website
from ...services.integrations import ga4
from ...services.integrations.google_oauth import get_access_token

router = APIRouter(prefix="/api", tags=["debug"])


@router.get("/websites/{website_id}/pages/{page_id}/debug")
def debug_page_seo(
    website_id: int,
    page_id: int,
    db: DbSession,
    _: CurrentUser,
) -> dict[str, Any]:
    """Inspect complete raw SEO evidence, signals, and detected issues for one page."""
    page = db.scalar(
        select(Page).where(Page.id == page_id, Page.website_id == website_id)
    )
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Page {page_id} not found on website {website_id}.",
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
        "response_time_ms": page.response_time_ms,
        "signals": {
            "title": title or None,
            "title_length": len(title),
            "meta_description": desc or None,
            "meta_description_length": len(desc),
            "canonical_url": page.canonical_url,
            "robots_meta": page.robots_directive,
            "x_robots_tag": page.x_robots_tag,
            "h1": page.h1,
            "h1_count": page.h1_count,
            "h2_count": page.h2_count,
            "h3_count": page.h3_count,
            "word_count": page.word_count,
            "internal_link_count": page.internal_link_count,
            "external_link_count": page.external_link_count,
            "inbound_internal_links": page.inbound_internal_links,
            "image_count": page.image_count,
            "missing_alt_count": page.missing_alt_count,
            "has_viewport": page.has_viewport,
            "has_structured_data": page.has_structured_data,
            "structured_data_types": page.structured_data_types or [],
            "has_open_graph": page.has_open_graph,
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
    website_id: int = Query(...),
    db: DbSession = None,
    _: CurrentUser = None,
) -> dict[str, Any]:
    """Inspect raw GA4 API query details, property ID validation, and metrics for a website."""
    website = db.scalar(select(Website).where(Website.id == website_id))
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Website {website_id} not found.",
        )

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
