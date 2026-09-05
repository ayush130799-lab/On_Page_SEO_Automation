"""Impact scoring — roadmap Feature 1 (§4).

Ranks *recommendations*, not pages: §4.4 requires a separate Search Performance Impact and User
Activity Impact for every proposed change, so that "rewrite the title" and "add alt text" on the
same page receive different, explainable priorities.
"""

from .catalog import CATALOG, RecommendationType, get as get_type, known_types
from .scoring import (
    DEFAULT_WEIGHTS,
    ImpactScore,
    activity_opportunity,
    business_relevance,
    confidence,
    expected_ctr,
    improvement_potential,
    score_recommendation,
    search_opportunity,
)

__all__ = [
    "CATALOG",
    "DEFAULT_WEIGHTS",
    "ImpactScore",
    "RecommendationType",
    "activity_opportunity",
    "business_relevance",
    "confidence",
    "expected_ctr",
    "get_type",
    "improvement_potential",
    "known_types",
    "score_recommendation",
    "search_opportunity",
]
