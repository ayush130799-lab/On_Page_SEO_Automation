"""The SEO Impact Scoring Engine — roadmap §4.3 and §4.4.

    Impact = Search Opportunity
           x User Activity Opportunity
           x SEO Improvement Potential
           x Business Relevance
           x Confidence          … normalised to 0-100

Every factor is computed from data the system already holds. Nothing here asks a language model
for a number: an LLM asked to "score the impact 0-100" produces a plausible-looking figure that
cannot be reproduced, audited, or recalibrated against outcomes, and §15 of the roadmap flags
exactly that as a risk. The AI's job is to write the *recommendation*; this module's job is to
rank it.

HOW THE PRODUCT IS NORMALISED
-----------------------------
Five factors each in 0-1 multiply out to a number that collapses towards zero (0.6^5 = 0.08), so
a literal product would compress every real page into the bottom of the scale and rank nothing.
The score is therefore the **weighted geometric mean** of the five factors, scaled to 0-100:

    score = 100 * Π(fᵢ ^ wᵢ)      where Σwᵢ = 1

This preserves the two properties the roadmap's formula is actually relying on — it is
multiplicative (a factor near zero drags the whole score down, which a weighted sum would not do)
and it is monotone in every factor — while keeping the output spread across the usable range.
The weights are configurable per website and are recorded on every score, so when §8.4's
post-deployment validation starts producing predicted-vs-actual data, they can be recalibrated
without touching this code.

TWO SCORES, NOT ONE
-------------------
§4.4 requires Search Performance Impact and User Activity Impact to be reported separately rather
than collapsed. They are computed as two independent passes over the same factors, differing in
which opportunity term leads and in the catalog leverage applied. ``overall_priority`` is the
blend, and all three are stored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import catalog

#: Default factor weights for the geometric mean. Search and activity opportunity dominate
#: because they measure *available* upside; improvement potential gates whether this particular
#: change can capture it; business relevance decides whether anyone should care; confidence
#: damps everything we are unsure about.
DEFAULT_WEIGHTS: dict[str, float] = {
    "search_opportunity": 0.28,
    "activity_opportunity": 0.22,
    "improvement_potential": 0.25,
    "business_relevance": 0.15,
    "confidence": 0.10,
}

#: A factor is never allowed to be exactly zero: one missing data source would otherwise zero the
#: whole score and make every page with partial data indistinguishable.
FACTOR_FLOOR = 0.05

#: Expected organic CTR by position — the curve used to judge whether a page under-performs the
#: click-through its ranking should already be earning.
_CTR_CURVE = [
    (1, 0.28), (2, 0.15), (3, 0.11), (4, 0.08), (5, 0.06),
    (6, 0.05), (7, 0.04), (8, 0.03), (9, 0.028), (10, 0.025),
]


@dataclass(slots=True)
class ImpactScore:
    """One recommendation's scores, with every input that produced them."""

    recommendation_type: str
    label: str

    search_impact_score: float          # 0-100, §4.4 A
    user_activity_score: float          # 0-100, §4.4 B
    business_impact_score: float        # 0-100
    overall_priority: float             # 0-100
    confidence_score: float             # 0-1

    factors: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    expected_outcome: str = ""
    effort: str = "medium"
    #: Which cost tier produced this score — see services.ai.tiering.
    tier: str = "statistical"
    evidence: dict[str, Any] = field(default_factory=dict)


# ── Factor computation ──────────────────────────────────────────────────────


def _clamp(value: float, low: float = FACTOR_FLOOR, high: float = 1.0) -> float:
    return max(low, min(high, value))


def expected_ctr(position: float | None) -> float:
    """The click-through rate a page at this position would normally earn."""
    if position is None or position <= 0:
        return 0.0
    if position >= 11:
        # Beyond page one, CTR decays slowly towards nothing.
        return max(0.002, 0.025 * (10.0 / position))
    lower = _CTR_CURVE[0][1]
    for pos, ctr in _CTR_CURVE:
        if position <= pos:
            return ctr
        lower = ctr
    return lower


def search_opportunity(metrics: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    """How much *unclaimed* search performance this URL has, 0-1.

    Opportunity is not the same as current performance. A page with 50,000 impressions at
    position 6 and a 1% CTR has enormous opportunity; the same page at position 2 with a 20% CTR
    has very little left to gain. The three components are demand (are there impressions to win),
    position headroom (is it close enough to page-one top to move), and the CTR gap (is it
    under-earning the clicks its position should already deliver).
    """
    metrics = metrics or {}
    impressions = float(metrics.get("impressions") or 0)
    clicks = float(metrics.get("clicks") or 0)
    position = metrics.get("position")
    evidence: dict[str, Any] = {"impressions": impressions, "clicks": clicks, "position": position}

    if impressions <= 0:
        # No search data at all — neutral, not zero. A page can be worth fixing before it ranks.
        evidence["basis"] = "no GSC impressions in window"
        return 0.45, evidence

    # Demand: log-scaled so 100 and 100,000 impressions are not 1000x apart in priority.
    demand = min(1.0, math.log10(impressions + 1) / 5.0)

    # Position headroom: positions 4-20 are "striking distance" — close enough that an on-page
    # change plausibly moves them onto page one, which is where clicks actually live.
    if position is None:
        headroom = 0.5
    elif position <= 3:
        headroom = 0.25          # already at the top; little left to win
    elif position <= 10:
        headroom = 1.0
    elif position <= 20:
        headroom = 0.85
    elif position <= 50:
        headroom = 0.45
    else:
        headroom = 0.20

    # CTR gap: the clearest form of unclaimed opportunity — the impressions already exist.
    ctr = (clicks / impressions) if impressions else 0.0
    target = expected_ctr(position)
    ctr_gap = 0.0
    if target > 0 and impressions >= 50:
        ctr_gap = _clamp((target - ctr) / target, 0.0, 1.0)
    evidence.update({"ctr": round(ctr, 4), "expected_ctr": round(target, 4),
                     "ctr_gap": round(ctr_gap, 3)})

    score = 0.40 * demand + 0.30 * headroom + 0.30 * ctr_gap
    evidence["basis"] = "GSC impressions, position and CTR gap"
    return _clamp(score), evidence


def activity_opportunity(metrics: dict[str, Any] | None) -> tuple[float, dict[str, Any]]:
    """How much on-site user activity is available to gain, 0-1.

    High traffic with weak engagement is the largest opportunity: the visitors are already
    arriving and something on the page is losing them. A page with no sessions at all has no
    activity opportunity to speak of until search fixes bring it traffic.
    """
    metrics = metrics or {}
    sessions = float(metrics.get("sessions") or 0)
    conversions = float(metrics.get("conversions") or 0)
    engagement = metrics.get("engagement_rate")
    evidence: dict[str, Any] = {
        "sessions": sessions, "conversions": conversions, "engagement_rate": engagement,
    }

    if sessions <= 0:
        evidence["basis"] = "no GA4 sessions in window"
        return 0.40, evidence

    traffic = min(1.0, math.log10(sessions + 1) / 4.0)

    # Engagement shortfall: the gap between this page's engagement and a healthy baseline.
    if engagement is None:
        shortfall = 0.5
    else:
        shortfall = _clamp((0.70 - float(engagement)) / 0.70, 0.0, 1.0)

    # Conversion shortfall: sessions arriving but not converting is unclaimed activity.
    conv_rate = conversions / sessions if sessions else 0.0
    conv_shortfall = _clamp((0.05 - conv_rate) / 0.05, 0.0, 1.0) if sessions >= 30 else 0.5

    score = 0.40 * traffic + 0.35 * shortfall + 0.25 * conv_shortfall
    evidence.update({"conversion_rate": round(conv_rate, 4), "basis": "GA4 traffic and engagement"})
    return _clamp(score), evidence


def improvement_potential(
    check_type: str,
    *,
    severity: str | None = None,
    issue_present: bool = True,
    current_state: str | None = None,
) -> tuple[float, dict[str, Any]]:
    """How much this *particular* change can improve the page, 0-1.

    This is the factor that makes two recommendations on the same page score differently, which
    is the whole point of §4.3. It combines the catalog ceiling for this kind of change with how
    badly the page currently fails it.
    """
    entry = catalog.get(check_type)
    evidence: dict[str, Any] = {
        "catalog_ceiling": entry.ceiling,
        "mechanism": entry.mechanism,
        "severity": severity,
    }

    if not issue_present:
        # Nothing wrong — there is no improvement to be had from this lever.
        evidence["basis"] = "no outstanding issue of this type"
        return FACTOR_FLOOR, evidence

    severity_factor = {
        "CRITICAL": 1.00,
        "HIGH": 0.80,
        "MEDIUM": 0.55,
        "LOW": 0.30,
    }.get((severity or "").upper(), 0.55)

    score = entry.ceiling * severity_factor
    evidence.update({"severity_factor": severity_factor, "basis": "catalog ceiling x severity"})
    if current_state:
        evidence["current_state"] = current_state[:200]
    return _clamp(score), evidence


def business_relevance(
    metrics: dict[str, Any] | None,
    *,
    site_revenue: float = 0.0,
    site_conversions: float = 0.0,
    path: str | None = None,
    high_value_patterns: tuple[str, ...] = (),
) -> tuple[float, dict[str, Any]]:
    """How much this URL matters to the business, 0-1.

    The roadmap assumes business relevance exists but never says where it comes from. Money is
    the least arguable source, so revenue share leads, then conversion share. Only when a site
    has no commercial telemetry at all do we fall back to URL patterns, which are configurable
    per website rather than guessed — a hardcoded list of "commercial-looking" paths would be
    exactly the kind of site-specific assumption that makes a tool wrong on someone else's site.
    """
    metrics = metrics or {}
    revenue = float(metrics.get("revenue") or 0)
    conversions = float(metrics.get("conversions") or 0)
    evidence: dict[str, Any] = {"revenue": revenue, "conversions": conversions}

    if site_revenue > 0 and revenue > 0:
        share = revenue / site_revenue
        # Square-root so a page with 4% of revenue is not written off against one with 16%.
        evidence["basis"] = "share of site revenue"
        evidence["revenue_share"] = round(share, 4)
        return _clamp(math.sqrt(min(1.0, share * 8))), evidence

    if site_conversions > 0 and conversions > 0:
        share = conversions / site_conversions
        evidence["basis"] = "share of site conversions"
        evidence["conversion_share"] = round(share, 4)
        return _clamp(math.sqrt(min(1.0, share * 8))), evidence

    if path and high_value_patterns:
        lowered = path.lower()
        for pattern in high_value_patterns:
            if pattern and pattern.lower() in lowered:
                evidence["basis"] = f"matched configured high-value path '{pattern}'"
                return 0.85, evidence

    evidence["basis"] = "no revenue, conversion or configured signal — neutral"
    return 0.50, evidence


def confidence(
    *,
    has_search_data: bool,
    has_activity_data: bool,
    impressions: float = 0.0,
    sessions: float = 0.0,
    method: str = "statistical",
) -> tuple[float, dict[str, Any]]:
    """How much to trust this score, 0-1.

    Confidence here means *data sufficiency*, not optimism about the outcome. §9.2 is explicit
    that the system should say "expected to improve" rather than promise a result, so this number
    describes the evidence behind the prediction and nothing more.
    """
    score = 0.35  # a rules-only judgement with no performance data at all
    evidence: dict[str, Any] = {"method": method}

    if has_search_data:
        score += 0.25 if impressions >= 100 else 0.15
    if has_activity_data:
        score += 0.20 if sessions >= 100 else 0.12
    if has_search_data and has_activity_data:
        score += 0.10  # corroborating sources

    if method == "ai":
        score += 0.05
    elif method == "rules":
        score -= 0.05

    evidence["basis"] = "data sufficiency across GSC and GA4"
    return _clamp(round(score, 3), 0.10, 0.95), evidence


# ── Composition ─────────────────────────────────────────────────────────────


def _geometric_mean(factors: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted geometric mean, 0-1. See the module docstring for why not a plain product."""
    total_weight = sum(weights.get(k, 0.0) for k in factors)
    if total_weight <= 0:
        return 0.0
    accumulated = 0.0
    for name, value in factors.items():
        weight = weights.get(name, 0.0)
        if weight <= 0:
            continue
        accumulated += weight * math.log(max(value, FACTOR_FLOOR))
    return math.exp(accumulated / total_weight)


def score_recommendation(
    check_type: str,
    *,
    metrics: dict[str, Any] | None = None,
    severity: str | None = None,
    issue_present: bool = True,
    current_state: str | None = None,
    recommended_state: str | None = None,
    site_revenue: float = 0.0,
    site_conversions: float = 0.0,
    path: str | None = None,
    high_value_patterns: tuple[str, ...] = (),
    weights: dict[str, float] | None = None,
    method: str = "statistical",
    tier: str = "statistical",
) -> ImpactScore:
    """Score one recommendation on one page against both §4.4 objectives."""
    entry = catalog.get(check_type)
    resolved_weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    metrics = metrics or {}

    search_opp, search_ev = search_opportunity(metrics)
    activity_opp, activity_ev = activity_opportunity(metrics)
    potential, potential_ev = improvement_potential(
        check_type, severity=severity, issue_present=issue_present, current_state=current_state
    )
    relevance, relevance_ev = business_relevance(
        metrics, site_revenue=site_revenue, site_conversions=site_conversions,
        path=path, high_value_patterns=high_value_patterns,
    )
    impressions = float(metrics.get("impressions") or 0)
    sessions = float(metrics.get("sessions") or 0)
    conf, conf_ev = confidence(
        has_search_data=impressions > 0,
        has_activity_data=sessions > 0,
        impressions=impressions,
        sessions=sessions,
        method=method,
    )

    factors = {
        "search_opportunity": search_opp,
        "activity_opportunity": activity_opp,
        "improvement_potential": potential,
        "business_relevance": relevance,
        "confidence": conf,
    }

    # ── §4.4 A: Search Performance Impact ───────────────────────────────────
    # The search pass leads with search opportunity and applies this change's search leverage;
    # activity opportunity is held at a neutral 0.5 so it cannot pull the search figure around.
    search_factors = dict(factors)
    search_factors["activity_opportunity"] = 0.50
    search_factors["improvement_potential"] = _clamp(potential * (0.35 + 0.65 * entry.search_leverage))
    search_score = round(_geometric_mean(search_factors, resolved_weights) * 100, 1)

    # ── §4.4 B: User Activity Impact ────────────────────────────────────────
    activity_factors = dict(factors)
    activity_factors["search_opportunity"] = 0.50
    activity_factors["improvement_potential"] = _clamp(potential * (0.35 + 0.65 * entry.activity_leverage))
    activity_score = round(_geometric_mean(activity_factors, resolved_weights) * 100, 1)

    # ── Business impact: the same lens, but weighted to what it is worth ────
    business_factors = dict(factors)
    business_score = round(
        _geometric_mean(business_factors, {**resolved_weights, "business_relevance": 0.45}) * 100, 1
    )

    # ── Overall: the blend the dashboard ranks on ───────────────────────────
    overall = round(_geometric_mean(factors, resolved_weights) * 100, 1)

    return ImpactScore(
        recommendation_type=entry.key,
        label=entry.label,
        search_impact_score=search_score,
        user_activity_score=activity_score,
        business_impact_score=business_score,
        overall_priority=overall,
        confidence_score=conf,
        factors={k: round(v, 3) for k, v in factors.items()},
        weights=resolved_weights,
        reason=_build_reason(entry, search_ev, activity_ev, potential_ev, relevance_ev),
        expected_outcome=_build_expected_outcome(entry, search_score, activity_score, search_ev),
        effort=entry.effort,
        tier=tier,
        evidence={
            "search": search_ev,
            "activity": activity_ev,
            "improvement": potential_ev,
            "business": relevance_ev,
            "confidence": conf_ev,
            "current_state": current_state,
            "recommended_state": recommended_state,
        },
    )


def _build_reason(
    entry: catalog.RecommendationType,
    search_ev: dict[str, Any],
    activity_ev: dict[str, Any],
    potential_ev: dict[str, Any],
    relevance_ev: dict[str, Any],
) -> str:
    """A data-backed explanation, per §9.1 — never a bare number."""
    parts: list[str] = []

    impressions = search_ev.get("impressions") or 0
    position = search_ev.get("position")
    ctr = search_ev.get("ctr")
    expected = search_ev.get("expected_ctr")

    if impressions >= 50 and position is not None:
        sentence = f"This URL receives {int(impressions):,} impressions at average position {position:.1f}"
        if ctr is not None and expected and ctr < expected:
            sentence += (
                f", with a {ctr * 100:.1f}% click-through rate against the {expected * 100:.1f}% "
                f"typical for that position"
            )
        parts.append(sentence + ".")
    elif impressions > 0:
        parts.append(f"This URL receives {int(impressions):,} search impressions.")
    else:
        parts.append("This URL has no search impressions recorded in the current window.")

    sessions = activity_ev.get("sessions") or 0
    if sessions > 0:
        engagement = activity_ev.get("engagement_rate")
        if engagement is not None:
            parts.append(
                f"It draws {int(sessions):,} sessions at a {float(engagement) * 100:.0f}% engagement rate."
            )
        else:
            parts.append(f"It draws {int(sessions):,} sessions.")

    parts.append(f"Fixing this matters because {entry.mechanism}.")

    basis = relevance_ev.get("basis")
    if basis and "no revenue" not in basis:
        parts.append(f"Business weighting: {basis}.")

    return " ".join(parts)


def _build_expected_outcome(
    entry: catalog.RecommendationType,
    search_score: float,
    activity_score: float,
    search_ev: dict[str, Any],
) -> str:
    """What we expect to happen — phrased as expectation, never as a guarantee (§9.2)."""
    ctr_gap = search_ev.get("ctr_gap") or 0
    impressions = search_ev.get("impressions") or 0

    if ctr_gap > 0.25 and impressions >= 100:
        recoverable = int(impressions * (search_ev.get("expected_ctr", 0) - search_ev.get("ctr", 0)))
        if recoverable > 0:
            return (
                f"Closing the click-through gap at the current ranking would be worth roughly "
                f"{recoverable:,} additional clicks per window if the snippet performs to the "
                f"position average. Not guaranteed — ranking and SERP layout can shift."
            )

    leading = "search performance" if search_score >= activity_score else "on-site user activity"
    return (
        f"Expected to improve {leading} for this URL. Search impact {search_score:.0f}/100, "
        f"user activity impact {activity_score:.0f}/100. SEO outcomes cannot be guaranteed; "
        f"treat this as a ranked expectation, not a promise."
    )
