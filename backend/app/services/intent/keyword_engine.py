"""Keyword opportunity engine (Phase 2).

Builds the five keyword tiers for each indexable page by combining:
  1. GSC queries already reaching the page (free, high signal)
  2. Semrush ranking keywords (free, from existing integration data)
  3. AI-generated keyword suggestions from the Phase 1 LLM payload

Computes the composite Keyword Opportunity Score per the roadmap formula:
  Score = Demand x RankingOpp x IntentMatch x BusinessRelevance x ContentRelevance x CompetitionOpp
  (each factor normalised 0–1, result scaled to 0–100)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

TIERS = ("primary", "secondary", "long_tail", "semantic", "question")

# Question starters that signal a "question" tier keyword
_QUESTION_PREFIXES = frozenset(
    {"how", "what", "when", "where", "why", "who", "which", "can", "is", "are",
     "does", "do", "should", "will", "has", "have"}
)

# Long-tail threshold: 3+ words is considered long-tail
_LONG_TAIL_MIN_WORDS = 3


@dataclass
class KeywordEntry:
    """One keyword with its tier assignment and opportunity scores."""

    keyword: str
    tier: str  # primary | secondary | long_tail | semantic | question
    source: str  # gsc | semrush | ai

    # Sub-scores (0–1 each)
    demand_score: float = 0.0
    ranking_opportunity_score: float = 0.0
    intent_match_score: float = 0.0
    business_relevance_score: float = 0.0
    content_relevance_score: float = 0.0
    competition_opportunity_score: float = 0.0

    # Composite (0–100)
    keyword_opportunity_score: float = 0.0

    # Known signals
    current_position: float | None = None
    current_impressions: int | None = None

    # Raw AI payload / metadata
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class KeywordTierResult:
    """All keyword entries for one page, grouped by tier."""

    keywords: list[KeywordEntry] = field(default_factory=list)
    page_keyword_opportunity_score: float = 0.0  # 0–100 aggregate for the page

    def by_tier(self, tier: str) -> list[str]:
        return [k.keyword for k in self.keywords if k.tier == tier]

    @property
    def primary(self) -> list[str]:
        return self.by_tier("primary")

    @property
    def secondary(self) -> list[str]:
        return self.by_tier("secondary")

    @property
    def long_tail(self) -> list[str]:
        return self.by_tier("long_tail")

    @property
    def semantic(self) -> list[str]:
        return self.by_tier("semantic")

    @property
    def question(self) -> list[str]:
        return self.by_tier("question")


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _rank_to_ranking_opportunity(position: float | None) -> float:
    """Convert a SERP position to a ranking opportunity score (0–1).

    Pages in positions 4–15 (striking distance) score highest because they are
    closest to the first-page top-3 where most clicks go.
    """
    if position is None:
        return 0.5  # unknown — assume moderate opportunity
    if position <= 3:
        return 0.10  # already at the top — limited headroom
    if position <= 10:
        return 0.70 + (10 - position) * 0.03  # strong striking distance
    if position <= 20:
        return 0.55 + (20 - position) * 0.015
    if position <= 50:
        return 0.30 + (50 - position) * 0.005
    return 0.15  # very deep — low opportunity


def _impressions_to_demand(impressions: int | None) -> float:
    """Normalise GSC impressions to a 0–1 demand signal using log scale."""
    if not impressions or impressions <= 0:
        return 0.30  # no data — assume average demand
    # log10(1000) ≈ 3.0 → 1.0; log10(10) ≈ 1.0 → 0.33
    return min(1.0, math.log10(impressions + 1) / 4.0)


def _intent_match_score(keyword_intent_guess: str, page_detected_intent: str) -> float:
    """Score how well a keyword's inferred intent aligns with the page intent."""
    if keyword_intent_guess == page_detected_intent:
        return 1.0
    # Partial match pairs
    # Adjacency, both ways round. The previous table was asymmetric by omission — an
    # informational keyword on a transactional page had no entry and fell to the 0.30 floor,
    # which is the score for a genuinely unrelated pairing.
    partial = {
        frozenset({"informational", "commercial"}): 0.60,
        frozenset({"transactional", "commercial"}): 0.80,
        frozenset({"transactional", "local"}): 0.70,
        frozenset({"commercial", "local"}): 0.65,
        frozenset({"informational", "local"}): 0.50,
        frozenset({"informational", "transactional"}): 0.35,
        frozenset({"navigational", "informational"}): 0.50,
        frozenset({"navigational", "transactional"}): 0.45,
        frozenset({"navigational", "commercial"}): 0.45,
    }
    return partial.get(frozenset({keyword_intent_guess, page_detected_intent}), 0.30)


#: Transactional stems, matched by prefix so "book" also catches "booking"/"booked"/"bookings".
#: Exact-token matching (what this used to do) classified "darshan booking" as informational,
#: which then scored the roadmap's own primary keyword at 0.30 intent match instead of 1.0.
_TRANSACTIONAL_STEMS = (
    "buy", "order", "book", "purchase", "hire", "rent", "price", "pricing", "cost",
    "checkout", "reserv", "subscrib", "ticket", "deal", "discount", "quote", "signup",
)
_COMMERCIAL_STEMS = (
    "best", "review", "compar", "versus", "top", "alternativ", "rating", "rank", "cheapest",
)
_LOCAL_STEMS = ("near", "nearby", "direction", "address", "location", "local")


def _stem_match(words: list[str], stems: tuple[str, ...]) -> bool:
    """True when any word starts with any stem."""
    return any(word.startswith(stem) for word in words for stem in stems)


def _keyword_intent_guess(keyword: str) -> str:
    """Fast heuristic for a single keyword's intent."""
    lowered = keyword.lower()
    words = lowered.split()
    if not words:
        return "informational"
    if words[0] in _QUESTION_PREFIXES:
        return "informational"
    if "near me" in lowered or _stem_match(words, _LOCAL_STEMS):
        return "local"
    if _stem_match(words, _TRANSACTIONAL_STEMS):
        return "transactional"
    if _stem_match(words, _COMMERCIAL_STEMS):
        return "commercial"
    return "informational"


def content_relevance(keyword: str, page_text: dict[str, str | None] | None) -> float:
    """How well the page already covers this keyword, 0-1.

    Computed from the crawled document, which the system already has — this used to be a flat
    0.70 for every keyword, which made a sixth of the roadmap's formula inert. Prominence is
    weighted: a term in the title means far more than the same term buried in body copy.
    """
    if not page_text:
        return 0.50  # page content unavailable — neutral rather than invented

    terms = [t for t in keyword.lower().split() if len(t) > 2]
    if not terms:
        return 0.50

    title = (page_text.get("title") or "").lower()
    h1 = (page_text.get("h1") or "").lower()
    headings = (page_text.get("headings") or "").lower()
    body = (page_text.get("content") or "").lower()

    score = 0.0
    for term in terms:
        if term in title:
            score += 0.40
        elif term in h1:
            score += 0.30
        elif term in headings:
            score += 0.18
        elif term in body:
            score += 0.10
    # Whole-phrase presence is a much stronger signal than the sum of its words.
    phrase = keyword.lower()
    if phrase in title or phrase in h1:
        score += 0.35
    elif phrase in body:
        score += 0.15

    return max(0.05, min(1.0, score / max(1, len(terms)) + (0.15 if phrase in body else 0.0)))


def competition_opportunity(
    position: float | None, difficulty: float | None, impressions: int | None
) -> float:
    """How winnable this keyword looks, 0-1.

    We have no third-party difficulty index for most keywords, so the honest proxy is: are we
    already visible for it (proof it is reachable), and where Semrush difficulty *is* present,
    use it. A flat constant — as this was — asserted a competition judgement we never made.
    """
    if difficulty is not None:
        # Semrush difficulty is 0-100 where higher is harder.
        return max(0.05, min(1.0, 1.0 - (float(difficulty) / 100.0)))
    if position is not None and position > 0:
        # Already ranking is direct evidence the keyword is winnable for this domain.
        if position <= 10:
            return 0.85
        if position <= 30:
            return 0.65
        return 0.45
    if impressions and impressions > 0:
        return 0.55  # visible at least sometimes
    return 0.40      # no evidence either way — below neutral, not above


def _composite_score(entry: KeywordEntry) -> float:
    """Apply the roadmap formula and scale to 0–100."""
    product = (
        entry.demand_score
        * entry.ranking_opportunity_score
        * entry.intent_match_score
        * entry.business_relevance_score
        * entry.content_relevance_score
        * entry.competition_opportunity_score
    )
    # geometric mean of 6 factors, scaled to 100
    return round(min(100.0, (product ** (1 / 6)) * 100), 1)


# ---------------------------------------------------------------------------
# Tier assignment
# ---------------------------------------------------------------------------

def _assign_tier(keyword: str, idx: int, source: str) -> str:
    """Assign a tier from the keyword's rank first, then its shape.

    Rank has to be checked *before* word count. Testing length first meant the roadmap's own
    stated primary keyword, "temple darshan booking", was filed as long-tail purely for being
    three words long — the strongest keyword on the page demoted by a word count.
    """
    words = keyword.lower().split()
    first = words[0] if words else ""

    # A question is a question regardless of rank; it belongs in its own tier.
    if first in _QUESTION_PREFIXES:
        return "question"

    # Rank: the best-performing real query, or the AI's first pick, is the primary keyword
    # however many words it happens to contain.
    if source in ("gsc", "semrush") and idx < 2:
        return "primary"
    if source == "ai" and idx == 0:
        return "primary"

    if len(words) >= _LONG_TAIL_MIN_WORDS:
        return "long_tail"
    if source == "ai":
        return "semantic"  # AI-generated non-question, non-long-tail
    return "secondary"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_keyword_tiers(
    page_url: str,
    detected_intent: str,
    gsc_queries: list[dict[str, Any]] | None = None,
    semrush_keywords: list[dict[str, Any]] | None = None,
    ai_keyword_tiers: list[dict[str, Any]] | None = None,
    page_text: dict[str, str | None] | None = None,
    business_relevance: float | None = None,
) -> KeywordTierResult:
    """Build the full keyword tier matrix for one page.

    Combines all available data sources, deduplicates, scores, and caps each
    tier at reasonable limits for storage and display.

    Args:
        page_url: Absolute URL (used for logging).
        detected_intent: Classified intent of the page.
        gsc_queries: GSC top-query rows (dict with ``query``, ``impressions``,
            ``clicks``, ``position``).
        semrush_keywords: Semrush ranking keyword rows (dict with ``keyword``,
            ``position``, ``volume``, ``difficulty``).
        ai_keyword_tiers: Keyword suggestions from the Phase 1 LLM response
            (list of dicts with ``keyword``, ``tier``, ``rationale``).

    Returns:
        A :class:`KeywordTierResult` with scored, deduplicated keyword entries.
    """
    entries: list[KeywordEntry] = []
    seen: set[str] = set()

    def _add(keyword: str, tier: str, source: str, difficulty: float | None = None, **kwargs) -> None:
        kw_norm = keyword.strip().lower()
        if not kw_norm or kw_norm in seen:
            return
        seen.add(kw_norm)
        entry = KeywordEntry(keyword=kw_norm, tier=tier, source=source, **kwargs)
        entry.intent_match_score = _intent_match_score(
            _keyword_intent_guess(kw_norm), detected_intent
        )
        # Business relevance is the *page's* commercial weight, supplied by the impact engine
        # from GA4 revenue/conversions. Deriving it from intent match (as this did) made two of
        # the six factors the same number wearing different names.
        entry.business_relevance_score = (
            business_relevance if business_relevance is not None else 0.50
        )
        entry.content_relevance_score = content_relevance(kw_norm, page_text)
        entry.competition_opportunity_score = competition_opportunity(
            entry.current_position, difficulty, entry.current_impressions
        )
        entry.keyword_opportunity_score = _composite_score(entry)
        entries.append(entry)

    # ── Source 1: GSC queries (high signal — real traffic data) ─────────────
    for idx, row in enumerate((gsc_queries or [])[:20]):
        kw = (row.get("query") or "").strip()
        if not kw:
            continue
        pos = row.get("position")
        imps = row.get("impressions")
        tier = _assign_tier(kw, idx, "gsc")
        _add(
            kw, tier, "gsc",
            current_position=float(pos) if pos is not None else None,
            current_impressions=int(imps) if imps is not None else None,
            demand_score=_impressions_to_demand(imps),
            ranking_opportunity_score=_rank_to_ranking_opportunity(pos),
        )

    # ── Source 2: Semrush keywords ───────────────────────────────────────────
    for idx, row in enumerate((semrush_keywords or [])[:20]):
        kw = (row.get("keyword") or "").strip()
        if not kw:
            continue
        pos = row.get("position")
        vol = row.get("volume") or row.get("search_volume")
        tier = _assign_tier(kw, idx, "semrush")
        _add(
            kw, tier, "semrush",
            difficulty=row.get("difficulty"),
            current_position=float(pos) if pos is not None else None,
            # Semrush reports monthly search volume; GSC reports impressions over the sync
            # window. Scaling volume by ~30 days puts both on the same footing before the
            # shared log-scaled demand curve is applied.
            demand_score=_impressions_to_demand(int(vol) * 30 if vol else None),
            ranking_opportunity_score=_rank_to_ranking_opportunity(pos),
        )

    # ── Source 3: AI suggestions (Phase 1 LLM payload) ──────────────────────
    for idx, item in enumerate((ai_keyword_tiers or [])[:25]):
        kw = (item.get("keyword") or "").strip()
        if not kw:
            continue
        ai_tier = (item.get("tier") or "").lower().replace(" ", "_")
        tier = ai_tier if ai_tier in TIERS else _assign_tier(kw, idx, "ai")
        _add(
            kw, tier, "ai",
            demand_score=0.55,  # unknown demand — moderate default
            ranking_opportunity_score=0.65,  # AI-suggested → likely untapped
            metadata={"rationale": item.get("rationale", "")},
        )

    # ── Cap each tier for storage efficiency ─────────────────────────────────
    TIER_CAPS = {
        "primary": 3,
        "secondary": 5,
        "long_tail": 8,
        "semantic": 8,
        "question": 6,
    }
    by_tier: dict[str, list[KeywordEntry]] = {t: [] for t in TIERS}
    for e in sorted(entries, key=lambda x: x.keyword_opportunity_score, reverse=True):
        if len(by_tier.get(e.tier, [])) < TIER_CAPS.get(e.tier, 10):
            by_tier.setdefault(e.tier, []).append(e)

    final = [e for tier_list in by_tier.values() for e in tier_list]

    # Page-level keyword opportunity = the mean of the best three keywords. A page is judged on
    # the keywords it can realistically win, not on the long tail of weak ones, so averaging
    # everything would penalise thorough keyword discovery.
    top3 = sorted(final, key=lambda x: x.keyword_opportunity_score, reverse=True)[:3]
    page_score = (
        round(sum(e.keyword_opportunity_score for e in top3) / len(top3), 1)
        if top3 else 0.0
    )

    logger.debug(
        "Built %d keyword entries for %s (page score %.1f)", len(final), page_url, page_score
    )
    return KeywordTierResult(keywords=final, page_keyword_opportunity_score=page_score)
