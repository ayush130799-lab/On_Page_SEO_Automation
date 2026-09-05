"""Intent mismatch detection (Phase 2).

Compares a page's *business intent* (inferred from its URL pattern — what the
page is *designed* to do) against the *query intent* that actually reaches it
via Google Search Console.

A mismatch means the page is attracting the wrong audience.  The most damaging
case is a transactional page (e.g. /darshan-booking) that ranks for
informational queries — it gets traffic but converts poorly because visitors
are not in booking mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .classifier import (
    CONFIDENCE_THRESHOLD,
    IntentClassificationResult,
    classify_by_statistics,
)
from .keyword_engine import _keyword_intent_guess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mismatch severity matrix
#
# Each cell is (business_intent, dominant_query_intent) → severity.
# Omitted pairs are treated as no-mismatch (None).
# ---------------------------------------------------------------------------

_SEVERITY_MATRIX: dict[tuple[str, str], str] = {
    # Transactional page ranking for informational queries — highest value leakage
    ("transactional", "informational"): "P0",
    # Transactional page ranking for navigational (brand ok, but generic nav isn't)
    ("transactional", "navigational"): "P1",
    # Commercial page ranking for informational — misses purchase intent
    ("commercial", "informational"): "P1",
    # Informational page attracting transactional queries it can't satisfy
    ("informational", "transactional"): "P1",
    # Local page ranking for generic (non-local) queries
    ("local", "informational"): "P2",
    ("local", "commercial"): "P2",
    # Navigational page ranking for commercial queries
    ("navigational", "commercial"): "P2",
    ("navigational", "transactional"): "P2",
}


@dataclass
class MismatchResult:
    """Outcome of the mismatch check for one page."""

    has_mismatch: bool
    severity: str | None  # P0 | P1 | P2 | P3 | None
    business_intent: str
    query_intent: str | None
    query_intent_confidence: float
    explanation: str
    #: "gsc_queries" | "page_targeting" | "none" — which evidence produced the verdict.
    evidence_source: str = "none"


def classify_targeting_intent(
    title: str | None,
    h1: str | None,
    headings: list[str] | None = None,
) -> IntentClassificationResult | None:
    """Infer intent from what the page *targets*, using its own title and headings.

    §6.4's worked example compares a page's purpose against "current keywords: temple history,
    temple architecture, temple facts" — the terms the page is written around, not the queries
    Google happens to send it. That distinction matters in practice: keying mismatch detection
    solely off GSC (as this module used to) meant it produced nothing at all for any site without
    a connected Search Console property, which is every site on its first crawl.
    """
    fragments = [f for f in [title, h1, *(headings or [])] if f]
    if not fragments:
        return None

    scores: dict[str, float] = {}
    # The title and H1 declare the page's subject; later headings elaborate on it, so they
    # carry progressively less weight.
    weights = [3.0, 2.0] + [1.0] * max(0, len(fragments) - 2)
    for fragment, weight in zip(fragments, weights):
        guess = _keyword_intent_guess(fragment)
        scores[guess] = scores.get(guess, 0.0) + weight

    total = sum(scores.values())
    if total <= 0:
        return None

    best = max(scores, key=lambda k: scores[k])
    share = scores[best] / total
    # Confidence tops out lower than the GSC-driven path: what a page says about itself is
    # weaker evidence than what users actually search to reach it.
    confidence = min(0.80, 0.45 + share * 0.45)
    return IntentClassificationResult(
        intent=best,
        confidence=round(confidence, 3),
        method="rules",
        signals=[f"page targeting: {best} ({share * 100:.0f}% of weighted title/heading signal)"],
    )


def detect_intent_mismatch(
    url: str,
    business_intent: str,
    gsc_queries: list[dict[str, Any]] | None,
    *,
    title: str | None = None,
    h1: str | None = None,
    headings: list[str] | None = None,
) -> MismatchResult:
    """Determine whether the page's purpose conflicts with what it actually targets or attracts.

    Two evidence sources, strongest first:

    1. **GSC queries** — what users actually search to arrive here. Direct evidence.
    2. **Page targeting** — what the title and headings are written around. Available on the
       first crawl, before any Search Console connection exists.

    Args:
        url: Page URL (for logging context).
        business_intent: The intent the page *should* serve (Level-1 rule result).
        gsc_queries: Top GSC query rows for the page.
        title / h1 / headings: The page's own copy, for the fallback evidence path.

    Returns:
        A :class:`MismatchResult` with severity and an actionable explanation.
    """
    stat: IntentClassificationResult | None = None
    evidence_source = "none"

    if gsc_queries:
        stat = classify_by_statistics(gsc_queries)
        if stat is not None and stat.confidence >= 0.55:
            evidence_source = "gsc_queries"
        else:
            stat = None

    if stat is None:
        stat = classify_targeting_intent(title, h1, headings)
        if stat is not None and stat.confidence >= 0.55:
            evidence_source = "page_targeting"
        else:
            stat = None

    if stat is None:
        return MismatchResult(
            has_mismatch=False,
            severity=None,
            business_intent=business_intent,
            query_intent=None,
            query_intent_confidence=0.0,
            explanation=(
                "Neither search queries nor page copy gave a strong enough intent signal to "
                "judge alignment."
            ),
            evidence_source="none",
        )

    query_intent = stat.intent
    severity = _SEVERITY_MATRIX.get((business_intent, query_intent))

    if severity is None:
        return MismatchResult(
            has_mismatch=False,
            severity=None,
            business_intent=business_intent,
            query_intent=query_intent,
            query_intent_confidence=stat.confidence,
            explanation=(
                f"Business intent ({business_intent}) aligns with observed intent ({query_intent})."
            ),
            evidence_source=evidence_source,
        )

    # Build a human-readable, actionable explanation
    explanation = _build_explanation(
        url, business_intent, query_intent, severity, gsc_queries, evidence_source, title, h1
    )

    logger.info(
        "Intent mismatch detected for %s — business=%s, query=%s, severity=%s",
        url, business_intent, query_intent, severity,
    )

    return MismatchResult(
        has_mismatch=True,
        severity=severity,
        business_intent=business_intent,
        query_intent=query_intent,
        query_intent_confidence=stat.confidence,
        explanation=explanation,
        evidence_source=evidence_source,
    )


def _build_explanation(
    url: str,
    business_intent: str,
    query_intent: str,
    severity: str,
    gsc_queries: list[dict[str, Any]] | None,
    evidence_source: str = "gsc_queries",
    title: str | None = None,
    h1: str | None = None,
) -> str:
    """Produce an actionable, data-backed mismatch explanation."""
    if evidence_source == "gsc_queries" and gsc_queries:
        top_queries = [q.get("query", "") for q in gsc_queries[:3] if q.get("query")]
        query_sample = ", ".join(f'"{q}"' for q in top_queries)
    else:
        # Quote the page's own copy, so the reader can see exactly what was judged.
        fragments = [f for f in (title, h1) if f]
        query_sample = ", ".join(f'"{f[:70]}"' for f in fragments) or "its current title and headings"

    # The templates below are written for the query path; on the targeting path the same
    # sentence has to describe the page's own copy or it misreports where the evidence came from.
    subject = (
        "the top queries reaching it"
        if evidence_source == "gsc_queries"
        else "its own title and headings"
    )

    templates: dict[tuple[str, str], str] = {
        ("transactional", "informational"): (
            f"This page is designed for {business_intent} actions (booking / purchase), but "
            f"{subject} — {query_sample} — are informational in nature. Visitors "
            f"arrive seeking information, not to complete a transaction, which depresses "
            f"conversion rates. Recommended fix: re-align <title>, <h1>, and opening copy to "
            f"high-intent transactional keywords; add prominent CTAs above the fold."
        ),
        ("transactional", "navigational"): (
            f"The page handles {business_intent} actions but is primarily reached via navigational "
            f"signals ({query_sample}). This points at a specific brand or site rather "
            f"than intending to complete a transaction. Add transactional keywords to the title "
            f"and meta description to capture users with purchase intent."
        ),
        ("commercial", "informational"): (
            f"This comparison / commercial page is framed informationally by {subject} ({query_sample}). "
            f"Users want general information rather than to evaluate purchase options. "
            f"Add a decision-focused CTA section and strengthen commercial signals in the <h1> "
            f"and meta description."
        ),
        ("informational", "transactional"): (
            f"This informational page carries transactional signals in {subject} ({query_sample}). "
            f"Users ready to buy or book are landing on content that doesn't fulfil that intent. "
            f"Add a clear conversion path (CTA / link to booking) to capture this high-intent traffic."
        ),
    }

    return templates.get(
        (business_intent, query_intent),
        (
            f"Business intent ({business_intent}) conflicts with the dominant query intent "
            f"({query_intent}). Evidence from {subject}: {query_sample}. Review <title>, <h1>, and "
            f"content framing to better align with the page's primary purpose."
        ),
    )
