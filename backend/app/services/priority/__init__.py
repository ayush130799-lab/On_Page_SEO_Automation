"""Business-priority scoring: which SEO problems matter most to fix."""

from .components import (
    ga4_activity_raw,
    gsc_search_raw,
    percentile_ranks,
    semrush_opportunity_raw,
    seo_severity_raw,
    severity_band,
)
from .engine import (
    PagePriority,
    ScoringResult,
    available_data_sources,
    compute_priorities,
    connected_providers,
    persist_priorities,
    score_website,
    top_priorities,
)
from .weights import (
    COMPONENTS,
    default_weights,
    normalise,
    redistribute,
    resolve_weights,
    set_weights,
    sub_weights,
)

__all__ = [
    "COMPONENTS",
    "PagePriority",
    "ScoringResult",
    "available_data_sources",
    "compute_priorities",
    "connected_providers",
    "default_weights",
    "ga4_activity_raw",
    "gsc_search_raw",
    "normalise",
    "percentile_ranks",
    "persist_priorities",
    "redistribute",
    "resolve_weights",
    "score_website",
    "semrush_opportunity_raw",
    "seo_severity_raw",
    "set_weights",
    "severity_band",
    "sub_weights",
    "top_priorities",
]
