"""Turn a crawl into a ranked, explained set of recommendations.

This is the "Opportunity Engine → Impact Scoring Engine → Priority Engine" run of §13, and it is
deliberately free: every score here comes from data already in the database, so a 10,000-page
site produces a complete ranked action plan without a single model call. Only after this ranking
exists does :mod:`app.services.ai.tiering` decide which handful of pages are worth AI wording.

All queries are bulk and chunked. The obvious implementation — loop the pages, query metrics per
page — costs three round trips per page and makes §12.3's 10,000-page requirement unmeetable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import (
    Page,
    PageIntentProfile,
    RecommendationScore,
    SEOIssue,
    Website,
)
from ...models.enums import Severity
from ..metrics import aggregate_page_metrics
from . import catalog
from .scoring import ImpactScore, score_recommendation

logger = logging.getLogger(__name__)

#: How many recommendation rows to keep per page. Beyond this the tail is noise — a page with 30
#: findings needs the top handful ordered, not all 30 ranked against each other.
MAX_SCORES_PER_PAGE = 12

#: `settings` row key holding impact factor weight overrides.
IMPACT_WEIGHTS_KEY = "impact_weights"


@dataclass
class ScoringOutcome:
    website_id: int
    pages_considered: int = 0
    pages_scored: int = 0
    recommendations_written: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    priority_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── §7.2 priority banding ───────────────────────────────────────────────────


def priority_level(
    *,
    overall: float,
    check_type: str,
    severity: str | None,
    metrics: dict[str, Any],
    intent_mismatch_severity: str | None = None,
) -> tuple[str, str]:
    """Assign P0-P3 and say why, using §7.2's explicit P0 criteria.

    The criteria are conditions, not score thresholds: a non-indexable page that happens to score
    62 is still a P0. Score-based banding only applies once none of them match.
    """
    impressions = float(metrics.get("impressions") or 0)
    clicks = float(metrics.get("clicks") or 0)
    position = metrics.get("position")
    ctr = (clicks / impressions) if impressions else None

    # 1. An important page that cannot be indexed at all.
    if check_type in ("http_status", "robots", "canonical_target") and severity == Severity.CRITICAL:
        return "P0", "page cannot be indexed or served correctly"

    # 2. A major intent mismatch.
    if intent_mismatch_severity == "P0":
        return "P0", "major search intent mismatch"

    # 3. A high-value page ranking poorly.
    if impressions >= 1000 and position is not None and position > 10:
        return "P0", f"high-demand page ranking at position {position:.0f}"

    # 4. Any other severe technical issue.
    if severity == Severity.CRITICAL:
        return "P0", "severe technical issue"

    # 5. A high-impression page with extremely poor click-through.
    if impressions >= 1000 and ctr is not None and ctr < 0.01:
        return "P0", f"{int(impressions):,} impressions at {ctr * 100:.1f}% CTR"

    if overall >= 70:
        return "P1", "strong opportunity for growth"
    if overall >= 45:
        return "P2", "useful optimisation with lower expected impact"
    return "P3", "minor improvement"


# ── Bulk loaders ────────────────────────────────────────────────────────────


def _issues_by_page(db: Session, page_ids: Sequence[int]) -> dict[int, list[SEOIssue]]:
    grouped: dict[int, list[SEOIssue]] = {pid: [] for pid in page_ids}
    if not page_ids:
        return grouped
    for chunk_start in range(0, len(page_ids), 500):
        chunk = list(page_ids)[chunk_start:chunk_start + 500]
        rows = db.scalars(
            select(SEOIssue).where(
                SEOIssue.page_id.in_(chunk), SEOIssue.is_resolved.is_(False)
            )
        ).all()
        for issue in rows:
            grouped.setdefault(issue.page_id, []).append(issue)
    return grouped


def _intent_by_page(db: Session, page_ids: Sequence[int]) -> dict[int, PageIntentProfile]:
    profiles: dict[int, PageIntentProfile] = {}
    if not page_ids:
        return profiles
    for chunk_start in range(0, len(page_ids), 500):
        chunk = list(page_ids)[chunk_start:chunk_start + 500]
        for profile in db.scalars(
            select(PageIntentProfile).where(PageIntentProfile.page_id.in_(chunk))
        ):
            profiles[profile.page_id] = profile
    return profiles


# ── Main entry point ────────────────────────────────────────────────────────


def score_website_recommendations(
    db: Session,
    website: Website,
    *,
    crawl_run_id: int | None = None,
    page_ids: Sequence[int] | None = None,
) -> ScoringOutcome:
    """Score every outstanding recommendation on a website and persist the ranking."""
    from ..ai.tiering import Tier, route_page, route_recommendation, summarise

    outcome = ScoringOutcome(website_id=website.id)

    query = select(Page).where(Page.website_id == website.id, Page.is_active.is_(True))
    if page_ids:
        query = query.where(Page.id.in_(list(page_ids)))
    pages = list(db.scalars(query))
    outcome.pages_considered = len(pages)
    if not pages:
        return outcome

    ids = [p.id for p in pages]
    metrics_by_page = aggregate_page_metrics(db, ids, window_days=settings.priority_metric_window_days)
    issues_by_page = _issues_by_page(db, ids)
    intents_by_page = _intent_by_page(db, ids)

    # Site totals for business relevance — computed once, not per page.
    site_revenue = sum(float(m.get("revenue") or 0) for m in metrics_by_page.values())
    site_conversions = sum(float(m.get("conversions") or 0) for m in metrics_by_page.values())
    high_value_paths = tuple(settings.business_value_paths or ())
    weights = _resolve_weights(db, website)

    # ── Pass 1: score everything, so pages can be ranked before AI routing ──
    per_page: dict[int, list[tuple[SEOIssue | None, ImpactScore]]] = {}
    for page in pages:
        metrics = metrics_by_page.get(page.id, {})
        profile = intents_by_page.get(page.id)
        scored: list[tuple[SEOIssue | None, ImpactScore]] = []

        for issue in issues_by_page.get(page.id, []):
            scored.append((
                issue,
                score_recommendation(
                    issue.check_type,
                    metrics=metrics,
                    severity=issue.severity,
                    issue_present=True,
                    current_state=_current_state(page, issue),
                    site_revenue=site_revenue,
                    site_conversions=site_conversions,
                    path=page.path,
                    high_value_patterns=high_value_paths,
                    weights=weights,
                    method="rules",
                ),
            ))

        # An intent mismatch is a recommendation in its own right, not an SEO rule failure.
        if profile is not None and profile.intent_mismatch:
            scored.append((
                None,
                score_recommendation(
                    "search_intent_mismatch",
                    metrics=metrics,
                    severity=_severity_for_mismatch(profile.mismatch_severity),
                    issue_present=True,
                    current_state=profile.mismatch_explanation,
                    site_revenue=site_revenue,
                    site_conversions=site_conversions,
                    path=page.path,
                    high_value_patterns=high_value_paths,
                    weights=weights,
                    method="statistical",
                ),
            ))

        # A page can under-earn clicks without failing any rule — that is pure §4.4 opportunity.
        ctr_score = _ctr_opportunity(page, metrics, site_revenue, site_conversions,
                                     high_value_paths, weights)
        if ctr_score is not None:
            scored.append((None, ctr_score))

        scored.sort(key=lambda pair: pair[1].overall_priority, reverse=True)
        per_page[page.id] = scored[:MAX_SCORES_PER_PAGE]

    # ── Pass 2: rank pages by their best recommendation, then route ─────────
    ranked = sorted(
        pages,
        key=lambda p: max((s.overall_priority for _, s in per_page.get(p.id, [])), default=0.0),
        reverse=True,
    )

    decisions = []
    tier_by_page: dict[int, Tier] = {}
    for rank, page in enumerate(ranked, start=1):
        scored = per_page.get(page.id, [])
        best = max((s.overall_priority for _, s in scored), default=0.0)
        profile = intents_by_page.get(page.id)
        decision = route_page(
            rank=rank,
            impact_score=best,
            has_critical_issue=page.highest_severity == Severity.CRITICAL,
            has_intent_mismatch=bool(profile and profile.intent_mismatch),
            issue_count=len(issues_by_page.get(page.id, [])),
        )
        decisions.append(decision)
        tier_by_page[page.id] = decision.tier

    outcome.tier_counts = summarise(decisions)

    # ── Pass 3: persist ─────────────────────────────────────────────────────
    db.execute(
        delete(RecommendationScore).where(RecommendationScore.page_id.in_(ids))
    )

    priority_counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    written = 0
    for page in pages:
        scored = per_page.get(page.id, [])
        if not scored:
            continue
        metrics = metrics_by_page.get(page.id, {})
        profile = intents_by_page.get(page.id)
        page_tier = tier_by_page.get(page.id, Tier.L2_STATISTICAL)

        for issue, score in scored:
            rec_tier = route_recommendation(
                score.recommendation_type,
                page_tier=page_tier,
                impact_score=score.overall_priority,
                severity=issue.severity if issue else None,
            )
            level, level_reason = priority_level(
                overall=score.overall_priority,
                check_type=score.recommendation_type,
                severity=issue.severity if issue else None,
                metrics=metrics,
                intent_mismatch_severity=profile.mismatch_severity if profile else None,
            )
            priority_counts[level] = priority_counts.get(level, 0) + 1

            db.add(
                RecommendationScore(
                    website_id=website.id,
                    page_id=page.id,
                    crawl_run_id=crawl_run_id,
                    recommendation_type=score.recommendation_type,
                    title=score.label,
                    current_state=(issue.description if issue else score.evidence.get("current_state")),
                    recommended_state=None,  # filled by the AI stage when the page reaches L3/L4
                    primary_keyword=_primary_keyword(profile),
                    secondary_keywords=(profile.secondary_keywords if profile else None),
                    search_intent=(profile.detected_intent if profile else None),
                    search_impact_score=score.search_impact_score,
                    user_activity_score=score.user_activity_score,
                    business_impact_score=score.business_impact_score,
                    overall_priority=score.overall_priority,
                    confidence_score=score.confidence_score,
                    priority_level=level,
                    severity=issue.severity if issue else None,
                    effort=score.effort,
                    reason=f"{score.reason} Priority {level}: {level_reason}.",
                    expected_outcome=score.expected_outcome,
                    tier=rec_tier.tier.value,
                    factors={"factors": score.factors, "weights": score.weights},
                    evidence=score.evidence,
                    status="detected",
                    scored_at=_now(),
                )
            )
            written += 1

        outcome.pages_scored += 1

    outcome.recommendations_written = written
    outcome.priority_counts = priority_counts
    db.flush()

    logger.info(
        "Impact scoring for website %s: %d pages, %d recommendations, tiers=%s, priorities=%s",
        website.id, outcome.pages_scored, written, outcome.tier_counts, priority_counts,
    )
    return outcome


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_weights(db: Session, website: Website) -> dict[str, float] | None:
    """Impact factor weights: environment default, then global, then per-website override.

    Mirrors services.priority.weights so an operator retunes impact scoring the same way they
    retune priority scoring — through a `settings` row, with no deploy and no number embedded at
    a call site.
    """
    from ...models import Setting

    resolved: dict[str, float] = {}
    for scope in (None, website.id):
        row = (
            db.query(Setting)
            .filter(
                Setting.key == IMPACT_WEIGHTS_KEY,
                Setting.website_id.is_(None) if scope is None else Setting.website_id == scope,
            )
            .one_or_none()
        )
        value = row.value if row else None
        if isinstance(value, dict):
            resolved.update({k: float(v) for k, v in value.items()})
    return resolved or None


def _severity_for_mismatch(mismatch_severity: str | None) -> str:
    return {
        "P0": Severity.CRITICAL,
        "P1": Severity.HIGH,
        "P2": Severity.MEDIUM,
    }.get(mismatch_severity or "", Severity.HIGH)


def _primary_keyword(profile: PageIntentProfile | None) -> str | None:
    if profile and profile.primary_keywords:
        return profile.primary_keywords[0]
    return None


def _current_state(page: Page, issue: SEOIssue) -> str | None:
    """What the page looks like now, for the recommendation's before/after (§11.1)."""
    snapshot = {
        "title": page.title,
        "meta_description": page.meta_description,
        "h1": page.h1,
        "content": f"{page.word_count} words" if page.word_count is not None else None,
        "canonical": page.canonical_url,
        "robots": page.robots_directive,
        "image_alt": (
            f"{page.missing_alt_count} of {page.image_count} images without alt text"
            if page.image_count else None
        ),
        "internal_links": (
            f"{page.internal_link_count} outgoing internal links"
            if page.internal_link_count is not None else None
        ),
    }.get(issue.check_type)
    return snapshot or issue.description


def _ctr_opportunity(
    page: Page,
    metrics: dict[str, Any],
    site_revenue: float,
    site_conversions: float,
    high_value_paths: tuple[str, ...],
    weights: dict[str, float] | None,
) -> ImpactScore | None:
    """A page under-earning clicks at its current ranking — §10.3's headline case.

    No SEO rule fails here: the title exists, the description exists, the page is indexable. The
    only evidence is the gap between actual and expected CTR, which is exactly the kind of
    opportunity a rule-based audit tool cannot see.
    """
    impressions = float(metrics.get("impressions") or 0)
    clicks = float(metrics.get("clicks") or 0)
    position = metrics.get("position")
    if impressions < 200 or position is None:
        return None

    from .scoring import expected_ctr

    ctr = clicks / impressions
    target = expected_ctr(position)
    if target <= 0 or ctr >= target * 0.75:
        return None  # performing acceptably for its position

    return score_recommendation(
        "ctr_opportunity",
        metrics=metrics,
        severity=Severity.HIGH if ctr < target * 0.4 else Severity.MEDIUM,
        issue_present=True,
        current_state=(
            f"{ctr * 100:.1f}% CTR at position {position:.1f} against a {target * 100:.1f}% expectation"
        ),
        site_revenue=site_revenue,
        site_conversions=site_conversions,
        path=page.path,
        high_value_patterns=high_value_paths,
        weights=weights,
        method="statistical",
    )
