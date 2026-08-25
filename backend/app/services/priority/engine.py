"""The priority engine.

Answers the platform's central question — *which SEO problems should we fix first?* — by scoring
every page on business importance rather than technical severity alone.

    priority_score = 100 × Σ( weightᵢ × percentile_rank(componentᵢ) )

Kept strictly separate from ``seo_score``: a page can be technically healthier than another and
still be the more urgent fix, because far more users, conversions and search demand run through it.
That inversion is the point of the whole system, and it is asserted directly in the tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...models import (
    GA4Metric,
    GSCMetric,
    Integration,
    IntegrationProvider,
    Page,
    PriorityScore,
    SemrushMetric,
    Website,
)
from ..metrics import aggregate_page_metrics
from .components import (
    ga4_activity_raw,
    gsc_search_raw,
    percentile_ranks,
    semrush_opportunity_raw,
    seo_severity_raw,
    severity_band,
)
from .weights import COMPONENTS, redistribute, resolve_weights

logger = logging.getLogger(__name__)


@dataclass
class PagePriority:
    """One page's computed priority, with everything needed to explain it."""

    page_id: int
    url: str
    score: float
    band: str = "P3"
    rank: int | None = None
    components: dict[str, float] = field(default_factory=dict)
    raw: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoringResult:
    website_id: int
    pages_scored: int
    weights: dict[str, float]
    data_sources: list[str]
    window_days: int
    computed_at: datetime
    priorities: list[PagePriority] = field(default_factory=list)


def available_data_sources(db: Session, website_id: int) -> set[str]:
    """Which signals this website actually has data for.

    Presence is judged on stored rows rather than integration status: a connection that has never
    completed a sync contributes nothing and must not dilute the weights of the signals that do
    exist.
    """
    sources = {"seo"}  # always present — the crawler is our own data

    checks = (
        ("ga4", GA4Metric),
        ("gsc", GSCMetric),
        ("semrush", SemrushMetric),
    )
    for name, model in checks:
        exists = db.scalar(
            select(model.id).where(model.website_id == website_id).limit(1)
        )
        if exists is not None:
            sources.add(name)

    return sources


def connected_providers(db: Session, website_id: int) -> set[str]:
    """Providers with a live connection, used for reporting rather than weighting."""
    rows = db.scalars(
        select(Integration.provider).where(
            Integration.website_id == website_id,
            Integration.status.in_(["connected", "syncing"]),
        )
    ).all()
    return {
        {IntegrationProvider.GSC: "gsc", IntegrationProvider.GA4: "ga4",
         IntegrationProvider.SEMRUSH: "semrush"}.get(provider, provider)
        for provider in rows
    }


def compute_priorities(
    db: Session,
    website: Website,
    *,
    window_days: int | None = None,
    weights: dict[str, float] | None = None,
) -> ScoringResult:
    """Score every active page on a website. Pure computation — nothing is written."""
    window = window_days or settings.priority_metric_window_days
    computed_at = datetime.now(timezone.utc)

    pages = db.scalars(
        select(Page).where(Page.website_id == website.id, Page.is_active.is_(True))
    ).all()

    configured = weights or resolve_weights(db, website.id)
    sources = available_data_sources(db, website.id)
    effective = redistribute(configured, sources)

    if not pages:
        return ScoringResult(
            website_id=website.id,
            pages_scored=0,
            weights=effective,
            data_sources=sorted(sources),
            window_days=window,
            computed_at=computed_at,
        )

    page_ids = [page.id for page in pages]
    metrics = aggregate_page_metrics(db, page_ids, window_days=window)

    # 1. Raw component values per page.
    raw_values: dict[str, list[float]] = {
        "seo_severity": [seo_severity_raw(page) for page in pages],
        "ga4_activity": [ga4_activity_raw(metrics.get(p.id, {})) for p in pages],
        "gsc_search": [gsc_search_raw(metrics.get(p.id, {})) for p in pages],
        "semrush_opportunity": [
            semrush_opportunity_raw(metrics.get(p.id, {})) for p in pages
        ],
    }

    # 2. Normalise each component to a within-site percentile rank.
    normalised = {
        component: percentile_ranks(values) for component, values in raw_values.items()
    }

    # 3. Weighted blend.
    priorities: list[PagePriority] = []
    for index, page in enumerate(pages):
        components = {
            component: round(normalised[component][index], 6) for component in COMPONENTS
        }
        score = round(
            100.0 * sum(effective[c] * components[c] for c in COMPONENTS), 2
        )
        priorities.append(
            PagePriority(
                page_id=page.id,
                url=page.url,
                score=score,
                components=components,
                raw={c: raw_values[c][index] for c in COMPONENTS},
                metrics=metrics.get(page.id, {}),
            )
        )

    # 4. Rank and band.
    priorities.sort(key=lambda p: (-p.score, p.page_id))
    distribution = [p.score for p in priorities]
    for rank, priority in enumerate(priorities, start=1):
        priority.rank = rank
        priority.band = severity_band(priority.score, distribution)

    return ScoringResult(
        website_id=website.id,
        pages_scored=len(priorities),
        weights=effective,
        data_sources=sorted(sources),
        window_days=window,
        computed_at=computed_at,
        priorities=priorities,
    )


def persist_priorities(db: Session, website: Website, result: ScoringResult) -> int:
    """Write ``PriorityScore`` rows and refresh each page's denormalised snapshot."""
    if not result.priorities:
        return 0

    pages = {
        page.id: page
        for page in db.scalars(
            select(Page).where(Page.id.in_([p.page_id for p in result.priorities]))
        )
    }

    for priority in result.priorities:
        page = pages.get(priority.page_id)
        if page is None:
            continue

        db.add(
            PriorityScore(
                website_id=website.id,
                page_id=page.id,
                score=priority.score,
                band=priority.band,
                rank=priority.rank,
                seo_severity_component=priority.components["seo_severity"],
                ga4_activity_component=priority.components["ga4_activity"],
                gsc_search_component=priority.components["gsc_search"],
                semrush_opportunity_component=priority.components["semrush_opportunity"],
                weights=result.weights,
                breakdown={
                    "raw": priority.raw,
                    "metrics": {
                        key: priority.metrics.get(key)
                        for key in (
                            "users", "sessions", "conversions", "revenue",
                            "clicks", "impressions", "ctr", "position",
                            "organic_keywords", "striking_distance_keywords",
                        )
                    },
                    "seo_score": page.seo_score,
                    "highest_severity": page.highest_severity,
                    "issue_count": page.issue_count,
                },
                data_sources=result.data_sources,
                metric_window_days=result.window_days,
                computed_at=result.computed_at,
            )
        )

        page.priority_score = priority.score
        page.priority_band = priority.band
        page.priority_rank = priority.rank

    website.last_scored_at = result.computed_at
    website.high_priority_page_count = sum(
        1 for p in result.priorities if p.band in ("P0", "P1")
    )
    db.commit()

    logger.info(
        "Scored %d pages for website %s (weights=%s, sources=%s).",
        result.pages_scored, website.id, result.weights, result.data_sources,
    )
    return result.pages_scored


def score_website(
    db: Session, website: Website, *, window_days: int | None = None
) -> ScoringResult:
    """Compute and persist priority scores for a website."""
    result = compute_priorities(db, website, window_days=window_days)
    persist_priorities(db, website, result)
    return result


def top_priorities(
    db: Session, website_id: int, limit: int = 20
) -> Sequence[Page]:
    """The highest-priority pages — the dashboard's "fix these first" list."""
    return db.scalars(
        select(Page)
        .where(
            Page.website_id == website_id,
            Page.is_active.is_(True),
            Page.priority_score.isnot(None),
        )
        .order_by(Page.priority_score.desc())
        .limit(limit)
    ).all()
