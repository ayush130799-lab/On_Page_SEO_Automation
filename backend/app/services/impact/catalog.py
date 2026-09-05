"""What kinds of recommendation exist, and which objective each one moves.

The roadmap's §4.3 example is the specification this file exists to satisfy: on a single page,
"improve title targeting booking intent" scores 94 while "add image ALT" scores 31. Those two
numbers cannot differ unless the engine knows something intrinsic about *the kind of change being
recommended* — how much of its benefit lands on search performance versus on-site user activity,
and how much headroom the change has at all.

That knowledge lives here, in one table, rather than being scattered through the scoring code or
guessed at by the LLM. Each entry says:

``search_leverage`` / ``activity_leverage``
    How strongly this change moves each of the two objectives in §4.4, 0-1. A title rewrite is
    almost purely a search-performance lever (it changes the SERP snippet); a CTA improvement is
    almost purely a user-activity lever; content depth moves both.

``ceiling``
    The most improvement potential this change can ever carry, 0-1. Alt text genuinely matters,
    but no amount of it turns a page into a top-3 result — capping it is what stops a site with
    400 missing-ALT images from burying the one broken canonical.

``effort``
    Rough implementation cost, surfaced to the roadmap generator in §7.3 so a sprint can be
    packed sensibly. It deliberately does **not** feed the impact score: impact is about outcome,
    not convenience.

Every ``check_type`` registered in the SEO rule registry maps to an entry here. Anything without
an explicit entry falls back to :data:`DEFAULT_ENTRY`, so adding a rule never breaks scoring — it
just scores neutrally until someone characterises it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecommendationType:
    """One kind of change the system can recommend."""

    key: str
    label: str
    #: 0-1 — share of this change's benefit that lands on search performance.
    search_leverage: float
    #: 0-1 — share that lands on on-site user activity.
    activity_leverage: float
    #: 0-1 — maximum improvement potential this change can carry.
    ceiling: float
    #: "low" | "medium" | "high" — implementation cost, for sprint planning only.
    effort: str
    #: Short statement of the mechanism, used in the generated `reason`.
    mechanism: str


DEFAULT_ENTRY = RecommendationType(
    key="other",
    label="Other improvement",
    search_leverage=0.50,
    activity_leverage=0.50,
    ceiling=0.50,
    effort="medium",
    mechanism="general on-page quality",
)


#: Keyed by the rule registry's ``check_type`` so the two stay in lockstep.
CATALOG: dict[str, RecommendationType] = {
    # ── Indexability: binary gates. If these fail nothing else can matter. ───
    "http_status": RecommendationType(
        "http_status", "Fix HTTP status", 1.00, 0.85, 1.00, "high",
        "a URL that does not return 200 cannot rank or convert at all",
    ),
    "robots": RecommendationType(
        "robots", "Fix robots directive", 1.00, 0.30, 1.00, "low",
        "a noindex directive removes the page from search entirely",
    ),
    "canonical": RecommendationType(
        "canonical", "Add canonical URL", 0.80, 0.05, 0.55, "low",
        "without a canonical, duplicate variants compete for the same rankings",
    ),
    "canonical_multiple": RecommendationType(
        "canonical_multiple", "Resolve conflicting canonicals", 0.90, 0.05, 0.75, "low",
        "conflicting canonicals are resolved arbitrarily by search engines",
    ),
    "canonical_target": RecommendationType(
        "canonical_target", "Correct canonical target", 0.90, 0.05, 0.80, "low",
        "a canonical pointing elsewhere de-indexes this URL in favour of another",
    ),
    "redirect_chain": RecommendationType(
        "redirect_chain", "Shorten redirect chain", 0.55, 0.35, 0.40, "medium",
        "each hop costs crawl budget and a little link equity, and slows the user",
    ),
    "url_structure": RecommendationType(
        "url_structure", "Improve URL structure", 0.40, 0.15, 0.25, "high",
        "readable URLs help relevance signals marginally; changing them risks redirects",
    ),

    # ── Metadata: the SERP snippet. Almost pure search leverage. ────────────
    "title": RecommendationType(
        "title", "Rewrite title tag", 1.00, 0.20, 0.95, "low",
        "the title is the strongest on-page relevance signal and the SERP headline",
    ),
    "title_multiple": RecommendationType(
        "title_multiple", "Remove duplicate title tags", 0.70, 0.05, 0.50, "low",
        "a second title tag makes the displayed headline unpredictable",
    ),
    "duplicate_title": RecommendationType(
        "duplicate_title", "Differentiate duplicate titles", 0.85, 0.10, 0.70, "medium",
        "pages sharing a title compete with each other for the same queries",
    ),
    "meta_description": RecommendationType(
        "meta_description", "Rewrite meta description", 0.85, 0.25, 0.65, "low",
        "the description drives SERP click-through without affecting ranking directly",
    ),
    "meta_description_multiple": RecommendationType(
        "meta_description_multiple", "Remove duplicate meta descriptions", 0.50, 0.05, 0.35, "low",
        "a second description tag makes the displayed snippet unpredictable",
    ),
    "duplicate_meta_description": RecommendationType(
        "duplicate_meta_description", "Differentiate duplicate descriptions", 0.55, 0.10, 0.40, "medium",
        "identical descriptions across pages waste the snippet's differentiating power",
    ),
    "open_graph": RecommendationType(
        "open_graph", "Add Open Graph metadata", 0.15, 0.55, 0.30, "low",
        "shared links render as rich cards, lifting referral click-through",
    ),
    "viewport": RecommendationType(
        "viewport", "Add mobile viewport", 0.55, 0.85, 0.60, "low",
        "without a viewport the page renders unusably on mobile, where most traffic is",
    ),

    # ── Headings & content: relevance and depth. Move both objectives. ──────
    "h1": RecommendationType(
        "h1", "Fix H1 heading", 0.85, 0.40, 0.80, "low",
        "the H1 confirms the page's topic to both readers and search engines",
    ),
    "heading_structure": RecommendationType(
        "heading_structure", "Improve subheading structure", 0.55, 0.55, 0.45, "medium",
        "subheadings carry secondary keywords and make long content scannable",
    ),
    "heading_depth": RecommendationType(
        "heading_depth", "Fix heading hierarchy", 0.30, 0.35, 0.20, "low",
        "skipped heading levels weaken document structure for assistive tech and parsers",
    ),
    "empty_headings": RecommendationType(
        "empty_headings", "Remove empty headings", 0.20, 0.20, 0.15, "low",
        "empty headings add structural noise without conveying anything",
    ),
    "content": RecommendationType(
        "content", "Expand or deepen content", 0.90, 0.80, 0.90, "high",
        "topical depth drives both ranking breadth and time on page",
    ),
    "duplicate_content": RecommendationType(
        "duplicate_content", "Resolve duplicate content", 0.85, 0.20, 0.70, "high",
        "near-identical pages split ranking signals between themselves",
    ),

    # ── Links ──────────────────────────────────────────────────────────────
    "internal_links": RecommendationType(
        "internal_links", "Add outgoing internal links", 0.60, 0.55, 0.45, "low",
        "internal links distribute authority and give readers a next step",
    ),
    "orphan_page": RecommendationType(
        "orphan_page", "Link to this orphan page", 0.85, 0.45, 0.75, "medium",
        "a page nothing links to is barely crawled and rarely found",
    ),
    "broken_links": RecommendationType(
        "broken_links", "Fix broken internal links", 0.50, 0.70, 0.45, "low",
        "broken links waste crawl budget and dead-end the reader",
    ),
    "external_links": RecommendationType(
        "external_links", "Review outbound links", 0.20, 0.20, 0.15, "low",
        "outbound links to quality sources support topical credibility",
    ),

    # ── Media ──────────────────────────────────────────────────────────────
    "image_alt": RecommendationType(
        "image_alt", "Add image alt text", 0.25, 0.30, 0.30, "low",
        "alt text serves accessibility and image search, but rarely moves page rankings",
    ),
    "image_dimensions": RecommendationType(
        "image_dimensions", "Set image dimensions", 0.20, 0.45, 0.25, "low",
        "explicit dimensions prevent layout shift, which affects Core Web Vitals",
    ),

    # ── Structured data & international ─────────────────────────────────────
    "structured_data": RecommendationType(
        "structured_data", "Add structured data", 0.60, 0.35, 0.50, "medium",
        "schema markup enables rich results, which lift SERP click-through",
    ),
    "hreflang": RecommendationType(
        "hreflang", "Fix hreflang annotations", 0.55, 0.25, 0.35, "medium",
        "hreflang routes each language's users to the right variant",
    ),

    # ── Intent & keyword recommendations (not rule-derived) ────────────────
    "search_intent_mismatch": RecommendationType(
        "search_intent_mismatch", "Re-align page with its search intent", 0.95, 0.95, 1.00, "high",
        "a page answering the wrong intent attracts traffic that cannot convert",
    ),
    "keyword_targeting": RecommendationType(
        "keyword_targeting", "Re-target primary keywords", 0.95, 0.45, 0.85, "medium",
        "aligning the page with the keywords it can realistically win concentrates its signals",
    ),
    "ctr_opportunity": RecommendationType(
        "ctr_opportunity", "Improve SERP click-through", 1.00, 0.55, 0.85, "low",
        "impressions already exist; a better snippet converts them into clicks with no ranking change",
    ),
    "cta_visibility": RecommendationType(
        "cta_visibility", "Improve call-to-action visibility", 0.10, 1.00, 0.85, "medium",
        "traffic that already arrives converts more often when the next step is obvious",
    ),
}


def get(check_type: str | None) -> RecommendationType:
    """The catalog entry for a rule check type, or a neutral default."""
    if not check_type:
        return DEFAULT_ENTRY
    return CATALOG.get(check_type, DEFAULT_ENTRY)


def known_types() -> list[str]:
    return sorted(CATALOG)
