"""Tiered AI cost routing — roadmap §12.3 and §12.4.

The rule the roadmap is emphatic about: crawl everything, but do not send everything to a model.
A 10,000-page site must not produce 10,000 LLM calls, and 500 pages sharing "missing alt text"
must not produce 500 separate AI analyses of the same trivial finding.

Four levels, cheapest first. Work stops at the first level that can answer honestly:

``L1_RULES``
    Deterministic checks already computed by the SEO rule engine. Free. A missing canonical does
    not need a language model to describe it, and the fix is the same on every site.

``L2_STATISTICAL``
    The impact scoring engine. Free — arithmetic over GSC/GA4 data already in the database. This
    is what ranks recommendations, so most pages get a complete, ordered action plan without any
    model call at all.

``L3_AI``
    A model call, for pages where contextual judgement genuinely changes the answer: content
    quality, keyword strategy, intent alignment, how to rewrite a title for *this* audience.

``L4_DEEP_AI``
    Reserved for the small set of pages where being wrong is expensive — the top of the priority
    ranking, confirmed intent mismatches, high-risk code changes. A larger model, more context.

Two independent gates apply. ``route_page`` decides how far a *page* is worth taking;
``route_recommendation`` decides per *finding*, so a high-value page still resolves its trivial
alt-text issue at L1 rather than spending model tokens on it. Every decision returns its reason,
and the chosen tier is stored on the resulting score, so routing can be audited after the fact
rather than inferred from an invoice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ...config import settings
from ..impact import catalog

logger = logging.getLogger(__name__)


class Tier(StrEnum):
    L1_RULES = "rules"
    L2_STATISTICAL = "statistical"
    L3_AI = "ai"
    L4_DEEP_AI = "deep_ai"


#: Findings whose fix is fully determined by the rule that found them. Sending these to a model
#: buys nothing: there is exactly one correct action and the rule already states it.
DETERMINISTIC_TYPES: frozenset[str] = frozenset({
    "image_alt",
    "image_dimensions",
    "viewport",
    "empty_headings",
    "heading_depth",
    "canonical_multiple",
    "meta_description_multiple",
    "title_multiple",
    "external_links",
    "hreflang",
    "url_structure",
    "http_status",
    "robots",
    "redirect_chain",
    "broken_links",
})

#: Findings where wording is the deliverable, so a model earns its cost.
CONTEXTUAL_TYPES: frozenset[str] = frozenset({
    "title",
    "meta_description",
    "h1",
    "heading_structure",
    "content",
    "duplicate_content",
    "duplicate_title",
    "duplicate_meta_description",
    "search_intent_mismatch",
    "keyword_targeting",
    "ctr_opportunity",
    "cta_visibility",
    "structured_data",
})


@dataclass(slots=True)
class TierDecision:
    """Which tier handles this unit of work, and why."""

    tier: Tier
    reason: str
    #: Populated for page-level decisions so the caller can report a ranking.
    rank: int | None = None

    @property
    def uses_ai(self) -> bool:
        return self.tier in (Tier.L3_AI, Tier.L4_DEEP_AI)


def route_page(
    *,
    rank: int,
    impact_score: float | None,
    has_critical_issue: bool,
    has_intent_mismatch: bool = False,
    issue_count: int = 0,
    max_ai_pages: int | None = None,
    max_deep_pages: int | None = None,
    deep_threshold: float | None = None,
    ai_threshold: float | None = None,
    ai_enabled: bool | None = None,
    force: bool = False,
) -> TierDecision:
    """Decide how far one page is worth taking, given its rank by impact.

    ``rank`` is 1-based position in the website's impact ordering. Ranking first and gating on
    rank is what makes the cost bounded: a site with 10,000 pages spends the same as a site with
    500, because the budget is a count, not a proportion.
    """
    ai_limit = settings.ai_max_pages if max_ai_pages is None else max_ai_pages
    deep_limit = settings.ai_deep_max_pages if max_deep_pages is None else max_deep_pages
    deep_cut = settings.ai_deep_impact_threshold if deep_threshold is None else deep_threshold
    ai_cut = settings.ai_impact_threshold if ai_threshold is None else ai_threshold
    # Explicit rather than read from global state inside the branch: routing is the one decision
    # in this system that spends money, so every input to it is visible in the signature.
    enabled = settings.ai_enabled if ai_enabled is None else ai_enabled

    if force:
        return TierDecision(Tier.L3_AI, "explicitly requested", rank)

    if issue_count == 0 and not has_intent_mismatch:
        return TierDecision(Tier.L1_RULES, "no outstanding issues", rank)

    if not enabled:
        return TierDecision(Tier.L2_STATISTICAL, "AI disabled by configuration", rank)

    score = impact_score if impact_score is not None else 0.0

    # ── Level 4: the few pages where being wrong is expensive ───────────────
    if rank <= deep_limit and (score >= deep_cut or has_intent_mismatch):
        why = (
            f"top {deep_limit} by impact with a confirmed intent mismatch"
            if has_intent_mismatch
            else f"top {deep_limit} by impact (score {score:.0f} >= {deep_cut:.0f})"
        )
        return TierDecision(Tier.L4_DEEP_AI, why, rank)

    # ── Level 3: worth a model call ─────────────────────────────────────────
    if rank <= ai_limit and (score >= ai_cut or has_critical_issue):
        why = (
            "carries a CRITICAL issue"
            if has_critical_issue and score < ai_cut
            else f"impact {score:.0f} >= {ai_cut:.0f} and inside the top {ai_limit}"
        )
        return TierDecision(Tier.L3_AI, why, rank)

    # ── Level 2: ranked and explained, at no model cost ─────────────────────
    if rank > ai_limit:
        return TierDecision(
            Tier.L2_STATISTICAL, f"outside the top {ai_limit} pages by impact", rank
        )
    return TierDecision(
        Tier.L2_STATISTICAL, f"impact {score:.0f} below the AI threshold of {ai_cut:.0f}", rank
    )


def route_recommendation(
    check_type: str,
    *,
    page_tier: Tier,
    impact_score: float,
    severity: str | None = None,
) -> TierDecision:
    """Decide the tier for one finding on a page already routed to ``page_tier``.

    A finding never exceeds its page's tier, and a deterministic finding never reaches a model
    even on a page that does — which is precisely §12.1's "500 pages with missing ALT" case.
    """
    entry = catalog.get(check_type)

    if check_type in DETERMINISTIC_TYPES:
        return TierDecision(
            Tier.L1_RULES,
            f"'{entry.label}' has a single determinate fix; no model input would change it",
        )

    if not page_tier in (Tier.L3_AI, Tier.L4_DEEP_AI):
        return TierDecision(Tier.L2_STATISTICAL, f"page resolved at {page_tier}")

    if check_type in CONTEXTUAL_TYPES:
        if page_tier is Tier.L4_DEEP_AI and impact_score >= settings.ai_deep_impact_threshold:
            return TierDecision(Tier.L4_DEEP_AI, "high-impact finding on a high-value page")
        return TierDecision(Tier.L3_AI, f"'{entry.label}' needs wording written for this page")

    return TierDecision(
        Tier.L2_STATISTICAL,
        f"'{entry.label}' is scored statistically; no contextual judgement required",
    )


def summarise(decisions: list[TierDecision]) -> dict[str, Any]:
    """Counts per tier, for the crawl summary and the cost report."""
    counts = {tier.value: 0 for tier in Tier}
    for decision in decisions:
        counts[decision.tier.value] += 1
    counts["ai_calls"] = counts[Tier.L3_AI.value] + counts[Tier.L4_DEEP_AI.value]
    return counts
