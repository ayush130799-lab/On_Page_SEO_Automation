"""Search intent classification engine (Phase 2).

Three-tier pipeline (rules → statistical → AI) that classifies every crawled
URL into one of five standard search intent categories:

    informational  — guides, blog posts, FAQs, educational content
    navigational   — brand queries, login, account, sitemap pages
    commercial     — comparison, review, best-X, pricing pages
    transactional  — booking, checkout, order, purchase pages
    local          — location-specific landing pages

The tier that first reaches CONFIDENCE_THRESHOLD wins; the AI tier is only
invoked when the cheaper tiers cannot confidently classify the page.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# A classification is considered confident enough to skip the next tier when
# its confidence meets or exceeds this threshold.
CONFIDENCE_THRESHOLD = 0.75

# ---------------------------------------------------------------------------
# URL pattern banks
# ---------------------------------------------------------------------------

# "darshan" was previously in this bank, taken from the roadmap's example client. It is a
# domain term, not a transactional signal, and it misclassified /darshan-timings and
# /darshan/history as transactional. A generic classifier must not carry one customer's
# vocabulary; site-specific terms belong in per-website configuration, not in the engine.
_TRANSACTIONAL_PATTERNS = re.compile(
    r"/(checkout|cart|basket|order|book(ing)?|purchase|buy|pay(ment)?|reservation|"
    r"reserve|subscribe|signup|sign-up|register|enroll|apply|appointment|schedule|"
    r"ticket|tickets|donate|quote-request)",
    re.IGNORECASE,
)

# `faq` used to sit here while the structured-data branch classified FAQPage as
# informational, so the same page got two different answers depending on which signal was
# read first. FAQs answer questions; they are informational.
_NAVIGATIONAL_PATTERNS = re.compile(
    r"/(login|log-in|logout|sign-in|signout|account|profile|dashboard|admin|"
    r"sitemap|robots\.txt|terms|privacy|cookie|contact|about|team|careers|"
    r"press|legal)",
    re.IGNORECASE,
)

_INFORMATIONAL_PATTERNS = re.compile(
    r"/(blog|article|news|post|guide|tutorial|how-to|howto|tips|learn|"
    r"education|resource|glossary|definition|wiki|what-is|why-is|faq|help|support|"
    r"introduction|overview|history|explained?|understanding|insight|timings?|hours)",
    re.IGNORECASE,
)

_COMMERCIAL_PATTERNS = re.compile(
    r"/(compare|comparison|review|rating|ranking|best-|vs\.?-|versus|"
    r"top-\d+|price|pricing|cost|alternative|option|feature|plan|"
    r"package|quote|estimate|deal|offer|discount)",
    re.IGNORECASE,
)

_LOCAL_PATTERNS = re.compile(
    r"/(location|directions?|map|near-me|local|city|region|state|"
    r"address|store|branch|dealer|showroom|outlet|nearme)",
    re.IGNORECASE,
)

# Words that strongly suggest intent category when found in top GSC queries
# Single tokens are matched against the query's word set; multi-word phrases have to be
# matched against the query *string*, because a set of split tokens can never contain
# "sign up" or "near me". Both banks are kept separate so each is matched correctly.
_INFORMATIONAL_QUERY_WORDS = frozenset(
    {"how", "what", "when", "where", "why", "who", "which", "guide", "tutorial",
     "learn", "understand", "explain", "definition", "meaning", "history", "about",
     "timings", "hours", "schedule"}
)
_TRANSACTIONAL_QUERY_WORDS = frozenset(
    {"buy", "book", "booking", "order", "purchase", "reserve", "booked", "hire", "rent",
     "download", "subscribe", "register", "apply", "price", "prices", "pricing",
     "cost", "cheap", "discount", "offer", "deal", "tickets", "ticket", "checkout"}
)
_COMMERCIAL_QUERY_WORDS = frozenset(
    {"best", "top", "review", "reviews", "compare", "comparison", "vs", "versus",
     "alternative", "alternatives", "recommended", "rating", "ranking", "list"}
)
_NAVIGATIONAL_QUERY_WORDS = frozenset(
    {"login", "account", "website", "official", "site", "portal", "contact"}
)
_LOCAL_QUERY_WORDS = frozenset(
    {"near", "nearby", "directions", "address", "location", "map", "local", "around"}
)

_INFORMATIONAL_PHRASES = ("how to", "what is", "why is", "how do", "how much time")
_TRANSACTIONAL_PHRASES = ("sign up", "best price", "book online", "buy online",
                          "order online", "book now")
_COMMERCIAL_PHRASES = ("best of", "vs.", "compared to")
_NAVIGATIONAL_PHRASES = ("sign in", "log in", "customer portal")
_LOCAL_PHRASES = ("near me", "close to me", "in my area", "opening hours")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class IntentClassificationResult:
    """Output from any classification tier."""

    intent: str  # one of the 5 INTENT_VALUES
    confidence: float  # 0.0 – 1.0
    method: str  # "rules" | "statistical" | "ai"
    signals: list[str] = field(default_factory=list)  # human-readable evidence


# ---------------------------------------------------------------------------
# Level 1 — Rule-based (deterministic, free)
# ---------------------------------------------------------------------------

def classify_by_rules(url: str, structured_data_types: list[str] | None, robots_directive: str | None) -> IntentClassificationResult | None:
    """Apply URL-pattern and structured-data heuristics.

    Returns *None* when no pattern matches (caller tries the next tier).
    """
    path = url.lower()
    signals: list[str] = []

    # A noindex directive says whether the page may be *indexed*; it says nothing about what a
    # user wants from it. Treating noindex as "navigational" (as this did) mislabelled every
    # staged or gated article and then suppressed the content recommendations that would have
    # been correct for it. Indexability is handled by the SEO rule engine, where it belongs.
    if robots_directive and "noindex" in robots_directive.lower():
        signals.append("noindex (recorded; does not determine intent)")

    # Structured data gives a very strong hint
    sd = [t.lower() for t in (structured_data_types or [])]
    if any(t in sd for t in ("product", "offer", "breadcrumb")):
        signals.append(f"structured_data: {sd}")
        if any(t in sd for t in ("offer",)):
            return IntentClassificationResult(intent="transactional", confidence=0.90, method="rules", signals=signals)
    if "article" in sd or "newsarticle" in sd or "blogposting" in sd:
        signals.append("structured_data: Article/BlogPosting")
        return IntentClassificationResult(intent="informational", confidence=0.90, method="rules", signals=signals)
    if "localbusiness" in sd or "restaurant" in sd or "hotel" in sd:
        signals.append("structured_data: LocalBusiness")
        return IntentClassificationResult(intent="local", confidence=0.90, method="rules", signals=signals)
    if "faqpage" in sd:
        signals.append("structured_data: FAQPage")
        return IntentClassificationResult(intent="informational", confidence=0.85, method="rules", signals=signals)

    # URL pattern matching (order: transactional first — most valuable to detect)
    if _TRANSACTIONAL_PATTERNS.search(path):
        signals.append(f"url matches transactional pattern")
        return IntentClassificationResult(intent="transactional", confidence=0.88, method="rules", signals=signals)
    if _LOCAL_PATTERNS.search(path):
        signals.append("url matches local pattern")
        return IntentClassificationResult(intent="local", confidence=0.82, method="rules", signals=signals)
    if _NAVIGATIONAL_PATTERNS.search(path):
        signals.append("url matches navigational pattern")
        return IntentClassificationResult(intent="navigational", confidence=0.86, method="rules", signals=signals)
    if _INFORMATIONAL_PATTERNS.search(path):
        signals.append("url matches informational pattern")
        return IntentClassificationResult(intent="informational", confidence=0.84, method="rules", signals=signals)
    if _COMMERCIAL_PATTERNS.search(path):
        signals.append("url matches commercial pattern")
        return IntentClassificationResult(intent="commercial", confidence=0.82, method="rules", signals=signals)

    # Root / homepage → navigational
    cleaned = re.sub(r"https?://[^/]+", "", path).rstrip("/")
    if cleaned in ("", "/"):
        return IntentClassificationResult(
            intent="navigational", confidence=0.80, method="rules", signals=["homepage"]
        )

    return None


# ---------------------------------------------------------------------------
# Level 2 — Statistical (data-driven, free)
# ---------------------------------------------------------------------------

def classify_by_statistics(gsc_queries: list[dict[str, Any]] | None) -> IntentClassificationResult | None:
    """Score intent from the actual queries that reach this page in GSC.

    If we have no query data, returns *None* (the AI tier must be used).
    """
    if not gsc_queries:
        return None

    # Count intent signals across the top queries, weighted by impressions. "local" was
    # previously absent from this dict, so the statistical tier could never return it however
    # local the queries were.
    scores: dict[str, float] = {
        "transactional": 0.0,
        "commercial": 0.0,
        "local": 0.0,
        "informational": 0.0,
        "navigational": 0.0,
    }
    banks = (
        ("informational", _INFORMATIONAL_QUERY_WORDS, _INFORMATIONAL_PHRASES),
        ("transactional", _TRANSACTIONAL_QUERY_WORDS, _TRANSACTIONAL_PHRASES),
        ("commercial", _COMMERCIAL_QUERY_WORDS, _COMMERCIAL_PHRASES),
        ("navigational", _NAVIGATIONAL_QUERY_WORDS, _NAVIGATIONAL_PHRASES),
        ("local", _LOCAL_QUERY_WORDS, _LOCAL_PHRASES),
    )

    total_impressions = 0
    for entry in gsc_queries[:20]:
        query = (entry.get("query") or "").lower()
        impressions = entry.get("impressions", 1) or 1
        total_impressions += impressions
        words = set(query.split())

        for name, tokens, phrases in banks:
            if (words & tokens) or any(phrase in query for phrase in phrases):
                scores[name] += impressions

    if total_impressions == 0 or max(scores.values()) == 0:
        return None

    # Ties are broken by the dict's declared order, which runs from the most commercially
    # consequential intent to the least. Relying on insertion order implicitly (as this did)
    # meant a transactional/commercial tie silently resolved differently after any edit.
    best_score = max(scores.values())
    best_intent = next(name for name, value in scores.items() if value == best_score)
    confidence = min(0.92, 0.5 + (best_score / total_impressions) * 0.6)

    if confidence < 0.50:
        return None

    return IntentClassificationResult(
        intent=best_intent,
        confidence=round(confidence, 3),
        method="statistical",
        signals=[
            f"top query intent signals: {best_intent} ({best_score:.0f}/{total_impressions:.0f} weighted impressions)"
        ],
    )


# ---------------------------------------------------------------------------
# Top-level classifier (orchestrates the tiers)
# ---------------------------------------------------------------------------

def classify_page_intent(
    url: str,
    structured_data_types: list[str] | None = None,
    robots_directive: str | None = None,
    gsc_queries: list[dict[str, Any]] | None = None,
    ai_detected_intent: str | None = None,
    ai_intent_confidence: float | None = None,
) -> IntentClassificationResult:
    """Run the tiered intent classification pipeline.

    Tiers stop as soon as confidence >= CONFIDENCE_THRESHOLD.  The AI result
    (from the Phase 1 LLM response) is accepted at Level 3 if both cheaper
    tiers failed.

    Args:
        url: Absolute URL of the page.
        structured_data_types: Structured data schema types detected on the page.
        robots_directive: robots meta / X-Robots-Tag value for the page.
        gsc_queries: Top GSC query rows for this page (dicts with ``query``,
            ``impressions``, ``clicks``, ``position``).
        ai_detected_intent: Intent already classified by the Phase 1 LLM call.
        ai_intent_confidence: Confidence from the LLM (0-1).

    Returns:
        The best :class:`IntentClassificationResult` this pipeline could produce.
    """
    # Level 1 — Rules
    result = classify_by_rules(url, structured_data_types, robots_directive)
    if result and result.confidence >= CONFIDENCE_THRESHOLD:
        logger.debug("Intent classified by rules: %s (%.0f%%) for %s", result.intent, result.confidence * 100, url)
        return result

    # Level 2 — Statistical
    stat_result = classify_by_statistics(gsc_queries)
    if stat_result and stat_result.confidence >= CONFIDENCE_THRESHOLD:
        logger.debug("Intent classified statistically: %s (%.0f%%) for %s", stat_result.intent, stat_result.confidence * 100, url)
        return stat_result

    # Level 3 — AI (reuse Phase 1 payload)
    if ai_detected_intent:
        confidence = float(ai_intent_confidence or 0.70)
        logger.debug("Intent classified by AI: %s (%.0f%%) for %s", ai_detected_intent, confidence * 100, url)
        return IntentClassificationResult(
            intent=ai_detected_intent,
            confidence=confidence,
            method="ai",
            signals=["intent from Phase 1 AI recommendation analysis"],
        )

    # Fallback: use whatever rules gave us, even if low confidence
    if result:
        return result
    if stat_result:
        return stat_result

    # Absolute fallback when nothing matched
    return IntentClassificationResult(
        intent="informational",
        confidence=0.40,
        method="rules",
        signals=["no pattern matched — defaulted to informational"],
    )


# ---------------------------------------------------------------------------
# Page type — the second classification axis required by §6.1
# ---------------------------------------------------------------------------

#: Commercial concepts, written as *stems* rather than inflected words. Listing "price",
#: "pricing" and "prices" separately would score one idea three times and let a single repeated
#: concept outvote a genuinely mixed page — which is exactly what a pricing guide is.
_COMMERCIAL_STEMS = frozenset(
    {"buy", "order", "book", "pric", "cost", "checkout", "cart", "subscrib", "trial",
     "demo", "quot", "purchas", "reserv", "ticket", "plan", "packag", "discount", "shop"}
)
_COMMERCIAL_PHRASES = ("add to cart", "get started", "sign up", "buy now", "book now")

#: Informational concepts, same convention.
_INFORMATIONAL_STEMS = frozenset(
    {"what", "why", "how", "guid", "learn", "histor", "explain", "overview",
     "introduc", "tip", "example", "mean", "defin", "faq", "question", "understand"}
)
_INFORMATIONAL_PHRASES = ("how to", "what is", "a guide to", "everything you need")

#: Schema types that assert a commercial purpose outright.
_COMMERCIAL_SCHEMA = frozenset({"product", "offer", "aggregateoffer", "service", "event"})
_INFORMATIONAL_SCHEMA = frozenset({"article", "blogposting", "newsarticle", "faqpage", "howto"})

PAGE_TYPES = ("commercial", "informational", "hybrid")


def classify_page_type(
    intent: str,
    *,
    title: str | None = None,
    h1: str | None = None,
    content: str | None = None,
    structured_data_types: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Return the §6.1 page type — commercial | informational | hybrid — and its evidence.

    This is a *separate axis* from search intent, not a relabelling of it. A pricing guide is
    informational in structure and commercial in purpose; §6.1 calls that hybrid, and collapsing
    it into one label is what causes the tool to recommend adding depth to a checkout page.

    Both sides are scored from the page's own words and schema rather than from the URL, because
    the URL already drove the intent classification and reusing it would just restate that answer.
    """
    signals: list[str] = []
    commercial = 0.0
    informational = 0.0

    schema = {t.lower() for t in (structured_data_types or [])}
    if schema & _COMMERCIAL_SCHEMA:
        commercial += 2.0
        signals.append(f"commercial schema: {sorted(schema & _COMMERCIAL_SCHEMA)}")
    if schema & _INFORMATIONAL_SCHEMA:
        informational += 2.0
        signals.append(f"informational schema: {sorted(schema & _INFORMATIONAL_SCHEMA)}")

    # Title and H1 carry far more intent than body copy, which mentions everything eventually.
    prominent = " ".join(filter(None, (title, h1))).lower()
    body = (content or "")[:4000].lower()

    prominent_words = re.findall(r"[a-z]+", prominent)
    body_words = re.findall(r"[a-z]+", body)

    def _weigh(stems: frozenset[str], phrases: tuple[str, ...]) -> float:
        """Score one vocabulary against the page. Each stem counts at most once.

        Title and H1 are weighted far above body copy: a page mentions everything eventually,
        but what it puts in its heading is what it is about.
        """
        score = 0.0
        for stem in stems:
            if any(word.startswith(stem) for word in prominent_words):
                score += 1.5
            elif any(word.startswith(stem) for word in body_words):
                score += 0.25
        for phrase in phrases:
            if phrase in prominent:
                score += 1.0
            elif phrase in body:
                score += 0.2
        return score

    commercial += _weigh(_COMMERCIAL_STEMS, _COMMERCIAL_PHRASES)
    informational += _weigh(_INFORMATIONAL_STEMS, _INFORMATIONAL_PHRASES)

    # The detected search intent is a prior, not the answer.
    if intent in ("transactional", "commercial"):
        commercial += 2.0
    elif intent == "informational":
        informational += 2.0
    elif intent == "local":
        commercial += 1.0

    signals.append(f"commercial={commercial:.1f} informational={informational:.1f}")

    total = commercial + informational
    if total <= 0:
        return "informational", signals + ["no signal — defaulted to informational"]

    ratio = commercial / total
    if ratio >= 0.65:
        return "commercial", signals
    if ratio <= 0.35:
        return "informational", signals
    return "hybrid", signals
