"""Traffic Potential and Lead Potential scores.

    Traffic Potential — "how much additional relevant organic traffic could this page capture?"
    Lead Potential    — "how much business/lead value could this page generate?"

Both are 0-100 and, like ``services/impact/scoring.py`` (whose factors this module reuses rather
than re-deriving), computed only from data the system already holds: GSC/GA4 aggregates and the
keyword/intent engine's own outputs. Nothing here asks a language model for a number, and nothing
is invented when a data source is missing — an unconnected integration degrades a component to a
neutral 0.5 rather than zeroing (or inflating) the whole score.
"""

from __future__ import annotations

from typing import Any

from .impact.scoring import business_relevance, search_opportunity

#: Search demand, ranking headroom and CTR gap (the three sub-factors ``search_opportunity``
#: already blends) cover the doc's Search Volume (25%) + Ranking Opportunity (25%) +
#: CTR Opportunity (15%) = 65%.
_TRAFFIC_WEIGHTS: dict[str, float] = {
    "search_opportunity": 0.65,
    "keyword_relevance": 0.20,
    "search_intent": 0.10,
    "page_quality": 0.05,
}

_LEAD_WEIGHTS: dict[str, float] = {
    "commercial_intent": 0.30,
    "traffic_potential": 0.25,
    "conversion_rate": 0.25,
    "engagement": 0.20,
}

#: Conversion rate treated as "fully captured" for the purposes of this score — the same benchmark
#: ``activity_opportunity`` uses for its own conversion-shortfall factor, so the two scores agree
#: on what a healthy conversion rate looks like.
_HEALTHY_CONVERSION_RATE = 0.05

#: How strongly the page's classified search intent suggests a lead/business outcome, absent any
#: other signal. Transactional and commercial pages are where leads are asked for; informational
#: pages occasionally convert but are not built to.
_INTENT_LEAD_LIKELIHOOD: dict[str, float] = {
    "transactional": 1.00,
    "commercial": 0.90,
    "local": 0.65,
    "navigational": 0.55,
    "informational": 0.30,
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_traffic_potential(
    *,
    metrics: dict[str, Any] | None,
    keyword_opportunity_score: float | None,
    intent_confidence: float | None,
    seo_score: float | None,
) -> tuple[float, dict[str, Any]]:
    """0-100 estimate of additional relevant organic traffic this page could capture.

    ``metrics`` is one page's entry from ``services.metrics.aggregate_page_metrics`` (GSC
    impressions/clicks/position). ``keyword_opportunity_score`` and ``intent_confidence`` come
    from that page's ``PageIntentProfile``. Every component is neutral (0.5), not zero, when its
    source is missing — an unconnected GSC integration must not make every page look like it has
    no traffic potential.
    """
    search_component, search_evidence = search_opportunity(metrics)
    keyword_component = (
        _clamp01(keyword_opportunity_score / 100.0) if keyword_opportunity_score is not None else 0.5
    )
    intent_component = _clamp01(intent_confidence) if intent_confidence is not None else 0.5
    quality_component = _clamp01(seo_score / 100.0) if seo_score is not None else 0.5

    components = {
        "search_opportunity": round(search_component, 4),
        "keyword_relevance": round(keyword_component, 4),
        "search_intent": round(intent_component, 4),
        "page_quality": round(quality_component, 4),
    }
    score = round(100.0 * sum(components[k] * _TRAFFIC_WEIGHTS[k] for k in _TRAFFIC_WEIGHTS), 1)
    return score, {
        "components": components,
        "weights": dict(_TRAFFIC_WEIGHTS),
        "search_evidence": search_evidence,
    }


def compute_lead_potential(
    *,
    traffic_potential_score: float,
    metrics: dict[str, Any] | None,
    detected_intent: str | None,
    site_revenue: float = 0.0,
    site_conversions: float = 0.0,
    path: str | None = None,
    high_value_patterns: tuple[str, ...] = (),
) -> tuple[float, dict[str, Any]]:
    """0-100 estimate of this page's potential to generate leads/business.

    Combines Traffic Potential with commercial intent (real revenue/conversion share, the same
    ``business_relevance`` factor the impact engine ranks recommendations with — see its
    docstring for why money leads over a hardcoded path guess), historical conversion rate and
    GA4 engagement.
    """
    metrics = metrics or {}
    relevance, relevance_evidence = business_relevance(
        metrics,
        site_revenue=site_revenue,
        site_conversions=site_conversions,
        path=path,
        high_value_patterns=high_value_patterns,
    )
    intent_likelihood = _INTENT_LEAD_LIKELIHOOD.get((detected_intent or "").lower(), 0.50)
    # Real business-relevance signal (money) leads; the classified intent nudges it rather than
    # overriding it, so a page with proven revenue is never discounted just because it read as
    # "informational".
    commercial_component = _clamp01(0.65 * relevance + 0.35 * intent_likelihood)

    sessions = float(metrics.get("sessions") or 0)
    conversions = float(metrics.get("conversions") or 0)
    conv_rate = (conversions / sessions) if sessions > 0 else None
    # Below 30 sessions a measured rate is mostly noise — neutral until there is enough traffic.
    if conv_rate is not None and sessions >= 30:
        conversion_component = _clamp01(conv_rate / _HEALTHY_CONVERSION_RATE)
    else:
        conversion_component = 0.5

    engagement = metrics.get("engagement_rate")
    engagement_component = _clamp01(engagement) if engagement is not None else 0.5

    traffic_component = _clamp01((traffic_potential_score or 0.0) / 100.0)

    components = {
        "commercial_intent": round(commercial_component, 4),
        "traffic_potential": round(traffic_component, 4),
        "conversion_rate": round(conversion_component, 4),
        "engagement": round(engagement_component, 4),
    }
    score = round(100.0 * sum(components[k] * _LEAD_WEIGHTS[k] for k in _LEAD_WEIGHTS), 1)
    return score, {
        "components": components,
        "weights": dict(_LEAD_WEIGHTS),
        "commercial_evidence": relevance_evidence,
        "conversion_rate": round(conv_rate, 4) if conv_rate is not None else None,
    }
