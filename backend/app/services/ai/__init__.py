"""AI recommendation engine: provider abstraction, structured schema and the selection gate."""

from .prompts import SYSTEM_PROMPT, build_user_prompt
from .providers import (
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    available_providers,
    extract_json,
    get_provider,
)
from .recommender import (
    AnalysisOutcome,
    SelectionDecision,
    analyse_page,
    analyse_website,
    cached_recommendation,
    persist_recommendation,
    select_pages,
)
from .schema import Finding, PageRecommendation, SuggestedChange

__all__ = [
    "AnalysisOutcome",
    "Finding",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponse",
    "PageRecommendation",
    "SYSTEM_PROMPT",
    "SelectionDecision",
    "SuggestedChange",
    "analyse_page",
    "analyse_website",
    "available_providers",
    "build_user_prompt",
    "cached_recommendation",
    "extract_json",
    "get_provider",
    "persist_recommendation",
    "select_pages",
]
