"""Weighted SEO health score.

The score is the weighted mean of every rule's 0-100 result. Weights come from configuration
(`SEO_WEIGHTS`, optionally overridden per website), falling back to the weight each rule declares
at registration — so a newly added rule is scored correctly without touching this module.
"""

from __future__ import annotations

from typing import Iterable

from ...config import settings
from ...models.enums import Severity, severity_rank
from .registry import RuleResult, registry


def resolve_weights(overrides: dict[str, float] | None = None) -> dict[str, float]:
    """Registry defaults ← global settings ← per-website overrides."""
    weights = registry.weights()
    weights.update(settings.seo_weights or {})
    if overrides:
        weights.update({k: float(v) for k, v in overrides.items()})
    return weights


def calculate_score(
    results: Iterable[RuleResult], weights: dict[str, float] | None = None
) -> float:
    """Weighted average of rule scores, rounded to one decimal place."""
    resolved = weights if weights is not None else resolve_weights()

    total_weight = 0.0
    weighted_sum = 0.0
    for result in results:
        # A check that never ran (page unreachable) is not evidence of health, so it is left out
        # of the average entirely rather than counted as a pass.
        if not getattr(result, "was_evaluated", True):
            continue
        weight = resolved.get(result.check_type, 0.0)
        if weight <= 0:
            continue
        total_weight += weight
        weighted_sum += float(result.score) * weight

    if total_weight <= 0:
        return 0.0
    return round(weighted_sum / total_weight, 1)


def determine_category(score: float) -> str:
    """Bucket a score into a health band.

    The boundaries are configurable; the defaults preserve the original behaviour where 90 is the
    top of the MEDIUM band, not the bottom of LOW.
    """
    if score > settings.seo_band_low_issues:
        return "LOW ISSUES"
    if score >= settings.seo_band_medium_issues:
        return "MEDIUM ISSUES"
    return "HIGH ISSUES"


def determine_highest_severity(results: Iterable[RuleResult]) -> str:
    """The most serious severity present among the failing rules."""
    highest = Severity.NONE
    best_rank = 0
    for result in results:
        if not result.is_issue:
            continue
        rank = severity_rank(result.severity)
        if rank > best_rank:
            best_rank = rank
            highest = (result.severity or Severity.NONE).upper()
    return str(highest)


def determine_priority_band(highest_severity: str) -> str:
    """Technical urgency band. CRITICAL → P0, HIGH → P1, MEDIUM → P2, otherwise P3."""
    return {
        Severity.CRITICAL: "P0",
        Severity.HIGH: "P1",
        Severity.MEDIUM: "P2",
    }.get((highest_severity or "").upper(), "P3")


def severity_counts(results: Iterable[RuleResult]) -> dict[str, int]:
    """Count failing rules by severity, always returning every key."""
    counts = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 0,
        Severity.MEDIUM: 0,
        Severity.LOW: 0,
    }
    for result in results:
        if result.is_issue:
            key = (result.severity or "").upper()
            if key in counts:
                counts[key] += 1
    return {str(k): v for k, v in counts.items()}
