"""Website Priority Matrix — roadmap §7.1.

For every URL: SEO Opportunity Score, User Activity Opportunity, Business Value, Technical
Severity, and Keyword Opportunity, combined into an Overall Priority Score. This is pure
aggregation over data Steps 1 and 2 already computed and persisted — no new external calls, no
new AI, matching the roadmap's own note that Feature 4 is "mostly aggregation... low net-new
complexity."

The five components are read from different places because they answer different questions:

* SEO / User Activity Opportunity — the best ``search_impact_score`` / ``user_activity_score``
  across the page's scored recommendations. "Best" rather than "average" because a page's
  opportunity is bounded by its single biggest lever, not diluted by its minor ones.
* Business Value — the same business-relevance figure the impact engine computed for this page
  (§4's definition), scaled to 0-100 for consistency with the other four components.
* Technical Severity — derived from the page's worst outstanding issue, not from any impact
  score. A CRITICAL issue on a page with modest traffic still needs fixing regardless of how
  little opportunity that traffic represents.
* Keyword Opportunity — the page's ``keyword_opportunity_score`` from the intent profile (§5.4),
  already 0-100.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Page, PageIntentProfile, RecommendationScore, Website
from ...models.enums import Severity

#: Equal weighting by default — the roadmap gives no reason to prefer one dimension over another
#: at the page-ranking level (unlike per-recommendation scoring, which does have that reason).
#: Configurable the same way impact weights are, through a `settings` row.
DEFAULT_MATRIX_WEIGHTS: dict[str, float] = {
    "seo_opportunity": 0.25,
    "user_activity_opportunity": 0.20,
    "business_value": 0.20,
    "technical_severity": 0.20,
    "keyword_opportunity": 0.15,
}

MATRIX_WEIGHTS_KEY = "priority_matrix_weights"

#: Severity → 0-100 technical-severity contribution. Matches severity_rank's ordering
#: (NONE < LOW < MEDIUM < HIGH < CRITICAL) rescaled onto the matrix's 0-100 range.
_SEVERITY_SCORE = {
    Severity.CRITICAL: 100.0,
    Severity.HIGH: 70.0,
    Severity.MEDIUM: 40.0,
    Severity.LOW: 15.0,
    Severity.NONE: 0.0,
}


@dataclass(slots=True)
class PriorityMatrixEntry:
    """One row of the §7.1 table."""

    page_id: int
    url: str
    path: str | None
    seo_opportunity: float
    user_activity_opportunity: float
    business_value: float
    technical_severity: float
    keyword_opportunity: float
    overall_priority: float
    priority_level: str
    top_recommendation: str | None = None
    top_recommendation_reason: str | None = None


def _bulk_best_recommendation_scores(
    db: Session, page_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """Per page: best search/user/business score, and the single top recommendation."""
    if not page_ids:
        return {}

    aggregates = {
        row[0]: (row[1], row[2], row[3])
        for row in db.execute(
            select(
                RecommendationScore.page_id,
                func.max(RecommendationScore.search_impact_score),
                func.max(RecommendationScore.user_activity_score),
                func.max(RecommendationScore.business_impact_score),
            )
            .where(RecommendationScore.page_id.in_(page_ids))
            .group_by(RecommendationScore.page_id)
        ).all()
    }

    # Group every recommendation by page so the matrix can report both "the top-scoring
    # recommendation" (for the why-column) and "the most urgent priority level" (for the band) —
    # these are not always the same row. impact.engine.priority_level already implements §7.2's
    # explicit P0 conditions (non-indexable, intent mismatch, high-impression/poor-CTR, …), and a
    # low-traffic page can carry a P0 by rule while scoring below a page with no P0 issue at all.
    # Re-deriving urgency from a fresh score threshold here would create a second, disagreeing
    # definition of P0.
    _LEVEL_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    by_page: dict[int, list[RecommendationScore]] = {}
    for row in db.scalars(
        select(RecommendationScore).where(RecommendationScore.page_id.in_(page_ids))
    ):
        by_page.setdefault(row.page_id, []).append(row)

    result: dict[int, dict[str, Any]] = {}
    for page_id, (search, activity, business) in aggregates.items():
        rows = by_page.get(page_id, [])
        top = max(rows, key=lambda r: r.overall_priority or 0.0, default=None)
        most_urgent = min(
            rows, key=lambda r: _LEVEL_RANK.get(r.priority_level or "P3", 3), default=None
        )
        result[page_id] = {
            "search": float(search or 0.0),
            "activity": float(activity or 0.0),
            "business": float(business or 0.0),
            "top_type": top.recommendation_type if top else None,
            "top_reason": top.reason if top else None,
            "priority_level": most_urgent.priority_level if most_urgent else None,
        }
    return result


def _bulk_keyword_scores(db: Session, page_ids: Sequence[int]) -> dict[int, float]:
    if not page_ids:
        return {}
    rows = db.execute(
        select(PageIntentProfile.page_id, PageIntentProfile.keyword_opportunity_score).where(
            PageIntentProfile.page_id.in_(page_ids)
        )
    ).all()
    return {page_id: float(score or 0.0) for page_id, score in rows}


def _resolve_matrix_weights(db: Session, website: Website) -> dict[str, float]:
    from ...models import Setting

    resolved = dict(DEFAULT_MATRIX_WEIGHTS)
    for scope in (None, website.id):
        row = (
            db.query(Setting)
            .filter(
                Setting.key == MATRIX_WEIGHTS_KEY,
                Setting.website_id.is_(None) if scope is None else Setting.website_id == scope,
            )
            .one_or_none()
        )
        if row and isinstance(row.value, dict):
            resolved.update({k: float(v) for k, v in row.value.items()})
    total = sum(resolved.values()) or 1.0
    return {k: v / total for k, v in resolved.items()}


def compute_priority_matrix(
    db: Session, website: Website, *, page_ids: Sequence[int] | None = None
) -> list[PriorityMatrixEntry]:
    """The full §7.1 table for a website, ranked by overall priority, most urgent first."""
    query = select(Page).where(Page.website_id == website.id, Page.is_active.is_(True))
    if page_ids:
        query = query.where(Page.id.in_(list(page_ids)))
    pages = list(db.scalars(query))
    if not pages:
        return []

    ids = [p.id for p in pages]
    rec_scores = _bulk_best_recommendation_scores(db, ids)
    keyword_scores = _bulk_keyword_scores(db, ids)
    weights = _resolve_matrix_weights(db, website)

    entries: list[PriorityMatrixEntry] = []
    for page in pages:
        rec = rec_scores.get(page.id, {})
        technical = _SEVERITY_SCORE.get(page.highest_severity or Severity.NONE, 0.0)
        business = rec.get("business", 0.0)
        keyword = keyword_scores.get(page.id, 0.0)
        seo_opp = rec.get("search", 0.0)
        activity_opp = rec.get("activity", 0.0)

        overall = (
            weights["seo_opportunity"] * seo_opp
            + weights["user_activity_opportunity"] * activity_opp
            + weights["business_value"] * business
            + weights["technical_severity"] * technical
            + weights["keyword_opportunity"] * keyword
        )

        # A page with no scored recommendations at all (nothing wrong, or not yet scored) has no
        # recommendation-derived band to inherit; fall back to the aggregate score so it still
        # sorts sensibly rather than defaulting to the least urgent bucket.
        level = rec.get("priority_level") or _band(overall)

        entries.append(
            PriorityMatrixEntry(
                page_id=page.id,
                url=page.url,
                path=page.path,
                seo_opportunity=round(seo_opp, 1),
                user_activity_opportunity=round(activity_opp, 1),
                business_value=round(business, 1),
                technical_severity=round(technical, 1),
                keyword_opportunity=round(keyword, 1),
                overall_priority=round(overall, 1),
                priority_level=level,
                top_recommendation=rec.get("top_type"),
                top_recommendation_reason=rec.get("top_reason"),
            )
        )

    entries.sort(key=lambda e: e.overall_priority, reverse=True)
    return entries


def _band(overall: float) -> str:
    """Fallback banding for a page with no scored recommendations to inherit a level from."""
    if overall >= 70:
        return "P0"
    if overall >= 50:
        return "P1"
    if overall >= 30:
        return "P2"
    return "P3"
