"""Search intent & keyword intelligence services (Phase 2)."""

from .analyser import IntentAnalysisOutcome, analyse_intent_for_website
from .cannibalization import CannibalizationGroup, CannibalizationPage, detect_cannibalization
from .classifier import IntentClassificationResult, classify_page_intent
from .keyword_engine import KeywordEntry, KeywordTierResult, build_keyword_tiers
from .mismatch import MismatchResult, detect_intent_mismatch

__all__ = [
    "CannibalizationGroup",
    "CannibalizationPage",
    "IntentAnalysisOutcome",
    "IntentClassificationResult",
    "KeywordEntry",
    "KeywordTierResult",
    "MismatchResult",
    "analyse_intent_for_website",
    "build_keyword_tiers",
    "classify_page_intent",
    "detect_cannibalization",
    "detect_intent_mismatch",
]
