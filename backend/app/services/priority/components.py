"""The four priority components, and the normalisation that makes them comparable.

Each component reduces a page to a raw number, then the whole set is converted to a 0-1 score by
**percentile rank within the website**. Absolute normalisation cannot work here: 500 monthly users
is a top page on a brochure site and noise on a marketplace, and a fixed divisor would rank one of
those two portfolios into a flat line. Percentile rank spreads every site's pages across the full
range, which is what makes cross-site comparison meaningful.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from typing import Any, Sequence

from ...models.enums import Severity, severity_rank
from .weights import sub_weights

#: Extra weight for the presence of a severe issue, on top of its rank.
SEVERITY_WEIGHT = {
    Severity.CRITICAL: 1.0,
    Severity.HIGH: 0.72,
    Severity.MEDIUM: 0.40,
    Severity.LOW: 0.15,
    Severity.NONE: 0.0,
}

#: Position bands score highest where movement is cheapest — page 2 rather than page 5.
STRIKING_DISTANCE_RANGE = (4, 20)


def percentile_ranks(values: Sequence[float]) -> list[float]:
    """Map each value to its percentile rank in ``values`` (0.0-1.0).

    Ties share a rank. An all-identical input returns 0.0 for every entry, which correctly
    contributes nothing: if every page has the same traffic, traffic cannot discriminate between
    them.
    """
    if not values:
        return []

    ordered = sorted(values)
    unique_count = len(set(ordered))
    if unique_count <= 1:
        return [0.0] * len(values)

    denominator = len(ordered) - 1
    ranks = []
    for value in values:
        # Index of the first occurrence, so equal values receive an equal rank.
        ranks.append(bisect_left(ordered, value) / denominator)
    return ranks


def log_scale(value: float) -> float:
    """Compress a long-tailed count before ranking.

    Page traffic is roughly power-law distributed; ranking on raw counts lets a handful of
    outliers dominate, while ranking on log counts keeps the middle of the distribution legible.
    """
    return math.log1p(max(0.0, float(value)))


# ── Raw component values ────────────────────────────────────────────────────


def seo_severity_raw(page: Any) -> float:
    """How badly this page is broken, from its worst issue and its overall score.

    Blends three signals so a page with one CRITICAL issue outranks a page with a long tail of
    LOW ones, without ignoring volume entirely.
    """
    severity = (getattr(page, "highest_severity", None) or Severity.NONE).upper()
    severity_score = SEVERITY_WEIGHT.get(severity, 0.0)

    seo_score = getattr(page, "seo_score", None)
    # A perfect page contributes 0; a zero-scoring page contributes 1.
    deficit = (100.0 - float(seo_score)) / 100.0 if seo_score is not None else 0.5

    issue_count = getattr(page, "issue_count", 0) or 0
    volume = min(1.0, issue_count / 12.0)

    return round(0.55 * severity_score + 0.35 * deficit + 0.10 * volume, 6)


def ga4_activity_raw(metrics: dict[str, Any]) -> float:
    """How much real user and business value flows through this page."""
    weights = sub_weights("ga4")
    return round(
        weights["users"] * log_scale(metrics.get("users", 0))
        + weights["sessions"] * log_scale(metrics.get("sessions", 0))
        + weights["conversions"] * log_scale(metrics.get("conversions", 0)) * 2.0
        + weights["revenue"] * log_scale(metrics.get("revenue", 0)),
        6,
    )


def gsc_search_raw(metrics: dict[str, Any]) -> float:
    """How much search demand this page already captures, and how much it is leaving behind."""
    weights = sub_weights("gsc")

    clicks = log_scale(metrics.get("clicks", 0))
    impressions = log_scale(metrics.get("impressions", 0))

    # A page ranking 8th has more to gain from a fix than one ranking 1st or 90th.
    position = metrics.get("position")
    position_value = 0.0
    if position is not None and position > 0:
        low, high = STRIKING_DISTANCE_RANGE
        if low <= position <= high:
            position_value = 1.0
        elif position < low:
            position_value = 0.35
        else:
            position_value = max(0.0, 1.0 - (position - high) / 60.0) * 0.5

    # Impressions without clicks mean the listing is being seen and skipped.
    ctr = metrics.get("ctr")
    raw_impressions = metrics.get("impressions", 0) or 0
    ctr_gap = 0.0
    if raw_impressions >= 100 and ctr is not None:
        expected = _expected_ctr(position)
        if expected and ctr < expected:
            ctr_gap = min(1.0, (expected - ctr) / expected)

    return round(
        weights["clicks"] * clicks
        + weights["impressions"] * impressions
        + weights["position"] * position_value * 5.0
        + weights["ctr_gap"] * ctr_gap * 5.0,
        6,
    )


def _expected_ctr(position: float | None) -> float:
    """A rough organic CTR curve, used only to detect a page under-performing its rank."""
    if position is None or position <= 0:
        return 0.0
    curve = {1: 0.28, 2: 0.15, 3: 0.11, 4: 0.08, 5: 0.06, 6: 0.05, 7: 0.04, 8: 0.03,
             9: 0.026, 10: 0.023}
    rounded = int(round(position))
    if rounded in curve:
        return curve[rounded]
    return 0.015 if rounded <= 20 else 0.005


def semrush_opportunity_raw(metrics: dict[str, Any]) -> float:
    """How much unclaimed organic upside Semrush sees for this page."""
    weights = sub_weights("semrush")
    return round(
        weights["keywords"] * log_scale(metrics.get("organic_keywords", 0))
        + weights["traffic"] * log_scale(metrics.get("organic_traffic", 0))
        + weights["striking_distance"] * log_scale(metrics.get("opportunity_volume", 0))
        + weights["backlinks"] * log_scale(metrics.get("backlinks", 0)),
        6,
    )


def severity_band(score: float, distribution: Sequence[float]) -> str:
    """Bucket a priority score into P0-P3 against the website's own distribution.

    A fixed cut-off (say "P0 above 80") would label an entire healthy site P3 and an entire broken
    one P0, which tells a developer nothing about where to start. Banding relative to the site
    always produces a workable shortlist.
    """
    if not distribution:
        return "P3"

    ordered = sorted(distribution, reverse=True)
    total = len(ordered)

    def cutoff(fraction: float) -> float:
        index = max(0, min(total - 1, int(total * fraction) - 1))
        return ordered[index]

    if total < 8:
        # Too few pages for percentiles to mean anything; fall back to absolute bands.
        if score >= 70:
            return "P0"
        if score >= 50:
            return "P1"
        if score >= 30:
            return "P2"
        return "P3"

    if score >= cutoff(0.05):
        return "P0"
    if score >= cutoff(0.20):
        return "P1"
    if score >= cutoff(0.50):
        return "P2"
    return "P3"
