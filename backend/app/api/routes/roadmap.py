"""Website-level SEO planning — roadmap §7 and §10.1.

Pure aggregation over what ``app.services.impact`` and ``app.services.intent`` already scored.
No new AI calls happen from this file.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from ...core.deps import DbSession, ReadableWebsite, WritableWebsite
from ...models import SeoRoadmap
from ...services.roadmap import compute_priority_matrix, generate_roadmap, latest_roadmap

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["roadmap"])


@router.get(
    "/websites/{website_id}/priority-matrix",
    summary="Website Priority Matrix (roadmap §7.1)",
)
def get_priority_matrix(website: ReadableWebsite, db: DbSession):
    """Every URL with its SEO Opportunity, User Activity Opportunity, Business Value, Technical
    Severity and Keyword Opportunity, combined into an Overall Priority Score. Computed live from
    the latest recommendation scores and intent profiles — always current, unlike a roadmap
    snapshot.
    """
    entries = compute_priority_matrix(db, website)
    return {
        "website_id": website.id,
        "total": len(entries),
        "items": [
            {
                "page_id": e.page_id,
                "url": e.url,
                "path": e.path,
                "seo_opportunity": e.seo_opportunity,
                "user_activity_opportunity": e.user_activity_opportunity,
                "business_value": e.business_value,
                "technical_severity": e.technical_severity,
                "keyword_opportunity": e.keyword_opportunity,
                "overall_priority": e.overall_priority,
                "priority_level": e.priority_level,
                "top_recommendation": e.top_recommendation,
                "top_recommendation_reason": e.top_recommendation_reason,
            }
            for e in entries
        ],
    }


def _serialise_roadmap(roadmap: SeoRoadmap) -> dict:
    return {
        "id": roadmap.id,
        "website_id": roadmap.website_id,
        "generated_at": roadmap.generated_at.isoformat() if roadmap.generated_at else None,
        "overview": {
            "overall_seo_opportunity": roadmap.overall_seo_opportunity,
            "organic_growth_opportunity": roadmap.organic_growth_opportunity,
            "user_activity_opportunity": roadmap.user_activity_opportunity,
            "priority_counts": {
                "P0": roadmap.critical_issue_count,
                "P1": roadmap.high_impact_count,
                "P2": roadmap.medium_impact_count,
                "P3": roadmap.low_impact_count,
            },
        },
        "weeks": roadmap.weeks,
        "priority_matrix": roadmap.priority_matrix,
    }


@router.get(
    "/websites/{website_id}/roadmap",
    summary="Latest Website SEO Roadmap (roadmap §7.3 / §10.1)",
)
def get_roadmap(website: ReadableWebsite, db: DbSession):
    """The most recently generated roadmap, or an explanatory 404-shaped response if none has
    been generated yet — call ``POST .../roadmap/generate`` first.
    """
    roadmap = latest_roadmap(db, website)
    if roadmap is None:
        return {
            "website_id": website.id,
            "generated": False,
            "message": "No roadmap has been generated yet. POST to /roadmap/generate first.",
        }
    return {"generated": True, **_serialise_roadmap(roadmap)}


@router.post(
    "/websites/{website_id}/roadmap/generate",
    summary="Generate a new Website SEO Roadmap snapshot",
)
def create_roadmap(website: WritableWebsite, db: DbSession):
    """Build a fresh roadmap from the current priority matrix and recommendation scores.

    A roadmap is a snapshot, not a live view — generate a new one when the team is ready to plan
    the next sprint, rather than having the plan reshuffle under them as scores update nightly.
    """
    roadmap = generate_roadmap(db, website)
    db.commit()
    logger.info(
        "Generated roadmap %s for website %s: %d weeks, %d matrix rows.",
        roadmap.id, website.id, len(roadmap.weeks), len(roadmap.priority_matrix),
    )
    return {"generated": True, **_serialise_roadmap(roadmap)}
