"""Website SEO Roadmap generation — roadmap §7.3 and the §10.1 overview.

Groups the site's already-ranked recommendations into weekly sprints, following §7.3's own
example shape:

    WEEK 1  the P0/P1 items — highest impact, fix first
    WEEK 2  P2 items — supporting optimisation, internal linking, keyword coverage
    WEEK 3  P3 items and low-effort technical cleanup — schema, images, polish

This is pure sequencing over what Steps 1-3 already computed. No new scoring happens here; a
roadmap is a *view* of the priority matrix and recommendation scores, organised for a team to
work through.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Page, RecommendationScore, SeoRoadmap, Website
from .matrix import PriorityMatrixEntry, compute_priority_matrix

#: How many recommendations to carry into each week. Weeks are not "the top N split three ways"
#: — they are shaped like §7.3's example: a short, focused week 1, a broader week 2, and a
#: cleanup-oriented week 3.
WEEK_CAPS = (10, 20, 30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _opportunity_band(score: float | None) -> str:
    """§10.1's LOW / MEDIUM / HIGH banding for the website overview."""
    if score is None:
        return "LOW"
    if score >= 65:
        return "HIGH"
    if score >= 35:
        return "MEDIUM"
    return "LOW"


def _website_overview(
    db: Session, website: Website, matrix: list[PriorityMatrixEntry]
) -> dict[str, Any]:
    """§10.1: Overall SEO Opportunity, Organic Growth Opportunity, User Activity Opportunity,
    and issue counts by priority level."""
    if not matrix:
        return {
            "overall_seo_opportunity": None,
            "organic_growth_opportunity": "LOW",
            "user_activity_opportunity": "LOW",
            "priority_counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
        }

    overall = round(sum(e.overall_priority for e in matrix) / len(matrix), 1)
    seo_avg = sum(e.seo_opportunity for e in matrix) / len(matrix)
    activity_avg = sum(e.user_activity_opportunity for e in matrix) / len(matrix)

    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for entry in matrix:
        counts[entry.priority_level] = counts.get(entry.priority_level, 0) + 1

    return {
        "overall_seo_opportunity": overall,
        "organic_growth_opportunity": _opportunity_band(seo_avg),
        "user_activity_opportunity": _opportunity_band(activity_avg),
        "priority_counts": counts,
    }


def _week_item(rec: RecommendationScore, url: str) -> dict[str, Any]:
    return {
        "recommendation_id": rec.id,
        "page_id": rec.page_id,
        "url": url,
        "recommendation_type": rec.recommendation_type,
        "title": rec.title,
        "priority_level": rec.priority_level,
        "overall_priority": rec.overall_priority,
        "search_impact_score": rec.search_impact_score,
        "user_activity_score": rec.user_activity_score,
        "confidence_score": rec.confidence_score,
        "effort": rec.effort,
        "reason": rec.reason,
        "expected_outcome": rec.expected_outcome,
    }


def _build_weeks(
    db: Session,
    website: Website,
    matrix: list[PriorityMatrixEntry] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Bucket recommendations into weeks by priority level, most urgent first within each week.

    Only ``detected`` and ``approved`` recommendations are scheduled — anything already
    ``implemented``, ``rejected``, ``in_progress`` or terminal doesn't belong in a forward plan.
    Falls back to priority matrix items if recommendation_scores are not yet populated.
    """
    rows = list(
        db.execute(
            select(RecommendationScore, Page.url)
            .join(Page, RecommendationScore.page_id == Page.id)
            .where(
                RecommendationScore.website_id == website.id,
                RecommendationScore.status.in_(("detected", "approved")),
            )
            .order_by(RecommendationScore.overall_priority.desc().nullslast())
        )
    )

    level_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    rows.sort(key=lambda pair: (level_rank.get(pair[0].priority_level or "P3", 3), -(pair[0].overall_priority or 0)))

    by_level: dict[str, int] = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for rec, _url in rows:
        by_level[rec.priority_level or "P3"] = by_level.get(rec.priority_level or "P3", 0) + 1

    week_defs = [
        (1, "Highest-impact fixes", ("P0", "P1")),
        (2, "Supporting optimisation and keyword coverage", ("P2",)),
        (3, "Technical cleanup and polish", ("P3",)),
    ]

    weeks: list[dict[str, Any]] = []
    used_ids: set[int] = set()

    if not rows and matrix:
        matrix_rows = sorted(
            [e for e in matrix if e.top_recommendation],
            key=lambda e: (level_rank.get(e.priority_level or "P3", 3), -(e.overall_priority or 0)),
        )
        for e in matrix_rows:
            by_level[e.priority_level or "P3"] = by_level.get(e.priority_level or "P3", 0) + 1

        for week_number, label, levels in week_defs:
            cap = WEEK_CAPS[week_number - 1]
            items = [
                {
                    "recommendation_id": -(idx + 1),
                    "page_id": e.page_id,
                    "url": e.url,
                    "recommendation_type": "technical_issue",
                    "title": e.top_recommendation,
                    "priority_level": e.priority_level,
                    "overall_priority": e.overall_priority,
                    "search_impact_score": e.seo_opportunity,
                    "user_activity_score": e.user_activity_opportunity,
                    "confidence_score": 0.8,
                    "effort": "medium",
                    "reason": e.top_recommendation_reason,
                    "expected_outcome": f"Resolves {e.priority_level} priority issues on {e.path} to improve search visibility.",
                }
                for idx, e in enumerate(matrix_rows)
                if e.priority_level in levels and e.page_id not in used_ids
            ][:cap]
            for it in items:
                if it["page_id"]:
                    used_ids.add(it["page_id"])
            tasks = [
                {
                    "action": it["title"],
                    "title": it["title"],
                    "page_id": it["page_id"],
                    "url": it["url"],
                    "priority": it["priority_level"],
                    "priority_level": it["priority_level"],
                    "rationale": it["reason"],
                    "reason": it["reason"],
                    "expected_outcome": it["expected_outcome"],
                    "effort": it["effort"],
                    "overall_priority": it["overall_priority"],
                    "search_impact_score": it["search_impact_score"],
                    "user_activity_score": it["user_activity_score"],
                    "recommendation_type": it.get("recommendation_type"),
                }
                for it in items
            ]
            weeks.append({
                "week": week_number,
                "label": label,
                "title": label,
                "focus": label,
                "items": items,
                "tasks": tasks,
            })
    else:
        for week_number, label, levels in week_defs:
            cap = WEEK_CAPS[week_number - 1]
            items = [
                _week_item(rec, url)
                for rec, url in rows
                if rec.priority_level in levels and rec.id not in used_ids
            ][:cap]
            for item in items:
                used_ids.add(item["recommendation_id"])
            tasks = [
                {
                    "action": it["title"],
                    "title": it["title"],
                    "page_id": it["page_id"],
                    "url": it["url"],
                    "priority": it["priority_level"],
                    "priority_level": it["priority_level"],
                    "rationale": it["reason"],
                    "reason": it["reason"],
                    "expected_outcome": it["expected_outcome"],
                    "effort": it["effort"],
                    "overall_priority": it["overall_priority"],
                    "search_impact_score": it["search_impact_score"],
                    "user_activity_score": it["user_activity_score"],
                    "recommendation_type": it.get("recommendation_type"),
                }
                for it in items
            ]
            weeks.append({
                "week": week_number,
                "label": label,
                "title": label,
                "focus": label,
                "items": items,
                "tasks": tasks,
            })

    return weeks, by_level


def generate_roadmap(
    db: Session, website: Website, *, crawl_run_id: int | None = None
) -> SeoRoadmap:
    """Build and persist a new roadmap snapshot for a website.

    Reuses whatever the impact engine and intent analyser already computed — this function makes
    no scoring decisions of its own, only sequencing ones.
    """
    matrix = compute_priority_matrix(db, website)
    overview = _website_overview(db, website, matrix)
    weeks, level_counts = _build_weeks(db, website, matrix)

    roadmap = SeoRoadmap(
        website_id=website.id,
        crawl_run_id=crawl_run_id,
        generated_at=_now(),
        overall_seo_opportunity=overview["overall_seo_opportunity"],
        organic_growth_opportunity=overview["organic_growth_opportunity"],
        user_activity_opportunity=overview["user_activity_opportunity"],
        critical_issue_count=level_counts.get("P0", 0),
        high_impact_count=level_counts.get("P1", 0),
        medium_impact_count=level_counts.get("P2", 0),
        low_impact_count=level_counts.get("P3", 0),
        weeks=weeks,
        priority_matrix=[asdict(e) for e in matrix],
    )
    db.add(roadmap)
    db.flush()
    return roadmap


def latest_roadmap(db: Session, website: Website) -> SeoRoadmap | None:
    return db.scalar(
        select(SeoRoadmap)
        .where(SeoRoadmap.website_id == website.id)
        .order_by(SeoRoadmap.generated_at.desc())
        .limit(1)
    )
