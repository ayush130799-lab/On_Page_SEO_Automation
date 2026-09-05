"""Impact scoring engine, cost tiering, intent classification and keyword opportunity.

Every one of these features shipped with zero test coverage. The assertions below are written
against the roadmap's own worked examples wherever it provides one, so a regression shows up as
"the spec's example no longer holds" rather than "a number changed".
"""

from __future__ import annotations

import pytest

from app.models.enums import Severity
from app.services.ai.tiering import (
    Tier,
    route_page,
    route_recommendation,
    summarise,
)
from app.services.impact import catalog
from app.services.impact.engine import priority_level
from app.services.impact.scoring import (
    activity_opportunity,
    business_relevance,
    confidence,
    expected_ctr,
    improvement_potential,
    score_recommendation,
    search_opportunity,
)
from app.services.intent.classifier import (
    classify_by_rules,
    classify_by_statistics,
    classify_page_intent,
    classify_page_type,
)
from app.services.intent.keyword_engine import (
    _keyword_intent_guess,
    build_keyword_tiers,
    competition_opportunity,
    content_relevance,
)
from app.services.intent.mismatch import detect_intent_mismatch

# A page with real search demand, poor CTR and commercial value — the §4.4 scenario.
HIGH_OPPORTUNITY = {
    "impressions": 48000, "clicks": 1000, "position": 6.2,
    "sessions": 4000, "conversions": 40, "revenue": 9000.0, "engagement_rate": 0.55,
}
NO_DATA: dict[str, float] = {}


# ── §4.3 / §4.4: two scores, and they must differ per recommendation ────────


class TestTwoSeparateImpactScores:
    def test_search_and_activity_scores_are_reported_separately(self):
        score = score_recommendation("title", metrics=HIGH_OPPORTUNITY, severity=Severity.HIGH)
        assert score.search_impact_score != score.user_activity_score
        assert 0 <= score.search_impact_score <= 100
        assert 0 <= score.user_activity_score <= 100

    def test_a_title_rewrite_is_a_search_lever_not_an_activity_lever(self):
        score = score_recommendation("title", metrics=HIGH_OPPORTUNITY, severity=Severity.HIGH)
        assert score.search_impact_score > score.user_activity_score

    def test_a_cta_change_is_an_activity_lever_not_a_search_lever(self):
        score = score_recommendation("cta_visibility", metrics=HIGH_OPPORTUNITY, severity=Severity.HIGH)
        assert score.user_activity_score > score.search_impact_score

    def test_overall_is_reported_alongside_both_not_instead_of_them(self):
        score = score_recommendation("title", metrics=HIGH_OPPORTUNITY, severity=Severity.HIGH)
        assert score.overall_priority > 0
        assert score.business_impact_score > 0
        assert 0.0 <= score.confidence_score <= 1.0

    def test_the_roadmap_ordering_holds_on_one_page(self):
        """§4.3: title > CTA > content > FAQ schema > alt text, on the same page."""
        page = dict(metrics=HIGH_OPPORTUNITY, severity=Severity.HIGH)
        title = score_recommendation("title", **page).overall_priority
        content = score_recommendation("content", **page).overall_priority
        schema = score_recommendation("structured_data", **page).overall_priority
        alt = score_recommendation("image_alt", **page).overall_priority

        assert title > schema > alt
        assert content > alt
        # The spread must be wide enough to actually rank with, not a two-point band.
        assert title - alt > 15

    def test_severity_moves_the_score_within_one_recommendation_type(self):
        critical = score_recommendation("h1", metrics=HIGH_OPPORTUNITY, severity=Severity.CRITICAL)
        low = score_recommendation("h1", metrics=HIGH_OPPORTUNITY, severity=Severity.LOW)
        assert critical.overall_priority > low.overall_priority

    def test_every_score_carries_a_reason_and_an_expected_outcome(self):
        """§9.1/§9.2 — a number is never shown without its explanation."""
        score = score_recommendation("title", metrics=HIGH_OPPORTUNITY, severity=Severity.HIGH)
        assert "48,000 impressions" in score.reason
        assert "position" in score.reason
        assert score.expected_outcome
        assert "guarantee" in score.expected_outcome.lower()

    def test_factors_and_weights_are_recorded_so_a_score_can_be_traced(self):
        score = score_recommendation("title", metrics=HIGH_OPPORTUNITY, severity=Severity.HIGH)
        assert set(score.factors) == {
            "search_opportunity", "activity_opportunity", "improvement_potential",
            "business_relevance", "confidence",
        }
        assert pytest.approx(sum(score.weights.values()), abs=0.001) == 1.0


# ── Individual factors ──────────────────────────────────────────────────────


class TestSearchOpportunity:
    def test_high_impressions_with_poor_ctr_is_the_biggest_opportunity(self):
        poor, _ = search_opportunity(
            {"impressions": 50000, "clicks": 250, "position": 6.0}
        )
        good, _ = search_opportunity(
            {"impressions": 50000, "clicks": 3000, "position": 2.0}
        )
        assert poor > good

    def test_striking_distance_beats_page_five(self):
        near, _ = search_opportunity({"impressions": 5000, "clicks": 50, "position": 7})
        far, _ = search_opportunity({"impressions": 5000, "clicks": 50, "position": 65})
        assert near > far

    def test_no_search_data_is_neutral_not_zero(self):
        score, evidence = search_opportunity(NO_DATA)
        assert 0.3 < score < 0.6
        assert "no GSC impressions" in evidence["basis"]

    def test_expected_ctr_declines_with_position(self):
        assert expected_ctr(1) > expected_ctr(5) > expected_ctr(10) > expected_ctr(30)
        assert expected_ctr(None) == 0.0


class TestActivityOpportunity:
    def test_traffic_with_weak_engagement_scores_above_traffic_with_strong(self):
        weak, _ = activity_opportunity({"sessions": 5000, "engagement_rate": 0.15, "conversions": 2})
        strong, _ = activity_opportunity({"sessions": 5000, "engagement_rate": 0.85, "conversions": 400})
        assert weak > strong

    def test_no_activity_data_is_neutral(self):
        score, evidence = activity_opportunity(NO_DATA)
        assert 0.3 < score < 0.6
        assert "no GA4 sessions" in evidence["basis"]


class TestImprovementPotential:
    def test_a_resolved_issue_has_no_potential_left(self):
        score, evidence = improvement_potential("title", severity=Severity.HIGH, issue_present=False)
        assert score <= 0.05
        assert "no outstanding issue" in evidence["basis"]

    def test_the_catalog_ceiling_separates_title_from_alt_text(self):
        title, _ = improvement_potential("title", severity=Severity.HIGH)
        alt, _ = improvement_potential("image_alt", severity=Severity.HIGH)
        assert title > alt

    def test_an_unknown_check_type_scores_neutrally_rather_than_failing(self):
        score, _ = improvement_potential("something_invented_later", severity=Severity.MEDIUM)
        assert 0 < score < 1


class TestBusinessRelevance:
    def test_revenue_share_leads(self):
        score, evidence = business_relevance(
            {"revenue": 5000.0}, site_revenue=20000.0
        )
        assert score > 0.5
        assert evidence["basis"] == "share of site revenue"

    def test_conversions_are_used_when_there_is_no_revenue(self):
        score, evidence = business_relevance(
            {"conversions": 30.0}, site_conversions=100.0
        )
        assert evidence["basis"] == "share of site conversions"
        assert score > 0.5

    def test_configured_paths_are_the_last_resort_not_the_first(self):
        score, evidence = business_relevance(
            {}, path="/pricing", high_value_patterns=("/pricing", "/checkout")
        )
        assert score == 0.85
        assert "configured high-value path" in evidence["basis"]

    def test_nothing_configured_and_no_telemetry_is_neutral_not_invented(self):
        score, evidence = business_relevance({}, path="/pricing", high_value_patterns=())
        assert score == 0.50
        assert "neutral" in evidence["basis"]


class TestConfidence:
    def test_more_data_sources_raise_confidence(self):
        both, _ = confidence(has_search_data=True, has_activity_data=True,
                             impressions=5000, sessions=5000)
        neither, _ = confidence(has_search_data=False, has_activity_data=False)
        assert both > neither

    def test_confidence_never_reaches_certainty(self):
        best, _ = confidence(has_search_data=True, has_activity_data=True,
                             impressions=1_000_000, sessions=1_000_000, method="ai")
        assert best <= 0.95


# ── §12.4: the tiered cost model ────────────────────────────────────────────


class TestCostTiering:
    def test_a_page_with_no_issues_never_reaches_a_model(self):
        decision = route_page(ai_enabled=True, rank=1, impact_score=99.0, has_critical_issue=False, issue_count=0)
        assert decision.tier is Tier.L1_RULES
        assert not decision.uses_ai

    def test_pages_outside_the_budget_are_still_scored_just_not_by_ai(self):
        decision = route_page(
            ai_enabled=True, rank=5000, impact_score=90.0,
            has_critical_issue=True, issue_count=4, max_ai_pages=200,
        )
        assert decision.tier is Tier.L2_STATISTICAL
        assert not decision.uses_ai
        assert "outside the top" in decision.reason

    def test_a_top_ranked_high_impact_page_earns_the_deep_tier(self):
        decision = route_page(ai_enabled=True, rank=1, impact_score=90.0, has_critical_issue=True, issue_count=5)
        assert decision.tier is Tier.L4_DEEP_AI

    def test_an_intent_mismatch_earns_the_deep_tier_regardless_of_score(self):
        decision = route_page(
            ai_enabled=True, rank=2, impact_score=20.0, has_critical_issue=False,
            has_intent_mismatch=True, issue_count=1,
        )
        assert decision.tier is Tier.L4_DEEP_AI
        assert "intent mismatch" in decision.reason

    def test_a_low_impact_page_inside_the_budget_stops_at_statistical(self):
        decision = route_page(ai_enabled=True, rank=10, impact_score=12.0, has_critical_issue=False, issue_count=3)
        assert decision.tier is Tier.L2_STATISTICAL

    def test_a_critical_issue_pulls_a_low_scoring_page_up_to_ai(self):
        decision = route_page(
            ai_enabled=True, rank=10, impact_score=12.0,
            has_critical_issue=True, issue_count=3,
        )
        assert decision.tier is Tier.L3_AI
        assert "CRITICAL" in decision.reason

    def test_deterministic_findings_never_reach_a_model_even_on_a_deep_page(self):
        """§12.1: 500 pages with missing alt text must not become 500 AI analyses."""
        decision = route_recommendation(
            "image_alt", page_tier=Tier.L4_DEEP_AI, impact_score=95.0
        )
        assert decision.tier is Tier.L1_RULES
        assert not decision.uses_ai

    def test_wording_findings_do_reach_a_model_on_an_ai_page(self):
        decision = route_recommendation("title", page_tier=Tier.L3_AI, impact_score=80.0)
        assert decision.tier is Tier.L3_AI

    def test_a_finding_never_exceeds_its_pages_tier(self):
        decision = route_recommendation("title", page_tier=Tier.L2_STATISTICAL, impact_score=95.0)
        assert not decision.uses_ai

    def test_summarise_counts_ai_calls(self):
        counts = summarise([
            route_page(ai_enabled=True, rank=1, impact_score=90, has_critical_issue=True, issue_count=2),
            route_page(ai_enabled=True, rank=9999, impact_score=90, has_critical_issue=True, issue_count=2),
            route_page(ai_enabled=True, rank=2, impact_score=0, has_critical_issue=False, issue_count=0),
        ])
        assert counts["ai_calls"] >= 1
        assert counts[Tier.L1_RULES.value] == 1


class TestCostBudgetIsBounded:
    def test_a_ten_thousand_page_site_costs_no_more_than_a_small_one(self):
        """§12.3 — the budget is a count, not a proportion of the site."""
        ai_routed = sum(
            1
            for rank in range(1, 10_001)
            if route_page(
                ai_enabled=True,
                rank=rank, impact_score=95.0, has_critical_issue=True, issue_count=5,
                max_ai_pages=200, max_deep_pages=20,
            ).uses_ai
        )
        assert ai_routed == 200


# ── §7.2 priority banding ───────────────────────────────────────────────────


class TestPriorityLevels:
    def test_a_non_indexable_page_is_p0_whatever_it_scores(self):
        level, reason = priority_level(
            overall=20.0, check_type="robots", severity=Severity.CRITICAL, metrics={}
        )
        assert level == "P0"
        assert "indexed" in reason

    def test_a_major_intent_mismatch_is_p0(self):
        level, reason = priority_level(
            overall=30.0, check_type="search_intent_mismatch", severity=None,
            metrics={}, intent_mismatch_severity="P0",
        )
        assert level == "P0"
        assert "intent mismatch" in reason

    def test_a_high_demand_page_ranking_poorly_is_p0(self):
        level, reason = priority_level(
            overall=40.0, check_type="title", severity=Severity.MEDIUM,
            metrics={"impressions": 5000, "clicks": 20, "position": 24.0},
        )
        assert level == "P0"

    def test_a_high_impression_page_with_dire_ctr_is_p0(self):
        level, reason = priority_level(
            overall=40.0, check_type="title", severity=Severity.MEDIUM,
            metrics={"impressions": 20000, "clicks": 40, "position": 4.0},
        )
        assert level == "P0"
        assert "CTR" in reason

    def test_ordinary_findings_band_by_score(self):
        assert priority_level(overall=85.0, check_type="title", severity=Severity.MEDIUM,
                              metrics={})[0] == "P1"
        assert priority_level(overall=55.0, check_type="title", severity=Severity.MEDIUM,
                              metrics={})[0] == "P2"
        assert priority_level(overall=20.0, check_type="image_alt", severity=Severity.LOW,
                              metrics={})[0] == "P3"


# ── §6: intent classification ───────────────────────────────────────────────


class TestIntentClassification:
    def test_all_five_categories_are_reachable(self):
        assert classify_by_rules("https://x.test/checkout", None, None).intent == "transactional"
        assert classify_by_rules("https://x.test/blog/guide", None, None).intent == "informational"
        assert classify_by_rules("https://x.test/login", None, None).intent == "navigational"
        assert classify_by_rules("https://x.test/compare-plans", None, None).intent == "commercial"
        assert classify_by_rules("https://x.test/store-locator/map", None, None).intent == "local"

    def test_no_client_specific_vocabulary_is_baked_into_the_engine(self):
        """A term from one customer's site must not classify pages on everyone else's."""
        result = classify_by_rules("https://x.test/darshan-timings", None, None)
        assert result is None or result.intent != "transactional"

    def test_noindex_does_not_decide_intent(self):
        """Indexability and intent are different questions."""
        with_noindex = classify_by_rules("https://x.test/blog/how-to-visit", None, "noindex, follow")
        without = classify_by_rules("https://x.test/blog/how-to-visit", None, None)
        assert with_noindex.intent == without.intent == "informational"

    def test_faq_is_informational_from_both_url_and_schema(self):
        assert classify_by_rules("https://x.test/faq", None, None).intent == "informational"
        assert classify_by_rules("https://x.test/anything", ["FAQPage"], None).intent == "informational"

    def test_the_statistical_tier_can_return_local(self):
        result = classify_by_statistics([
            {"query": "temple near me", "impressions": 900, "clicks": 5, "position": 6},
            {"query": "temple directions", "impressions": 700, "clicks": 4, "position": 8},
        ])
        assert result is not None and result.intent == "local"

    def test_multi_word_signal_phrases_are_matched(self):
        result = classify_by_statistics(
            [{"query": "clinic near me", "impressions": 1000, "clicks": 5, "position": 5}]
        )
        assert result is not None and result.intent == "local"

    def test_an_unclassifiable_page_falls_back_transparently(self):
        result = classify_page_intent("https://x.test/services/consulting")
        assert result.intent in {"informational", "commercial", "navigational"}
        assert result.confidence <= 0.6
        assert result.signals


class TestPageType:
    """§6.1's second axis, which was entirely missing."""

    def test_a_checkout_page_is_commercial(self):
        page_type, _ = classify_page_type(
            "transactional", title="Buy Tickets Online", h1="Checkout",
            structured_data_types=["Offer"],
        )
        assert page_type == "commercial"

    def test_a_blog_post_is_informational(self):
        page_type, _ = classify_page_type(
            "informational", title="What is a temple darshan?", h1="A guide to darshan",
            structured_data_types=["Article"],
        )
        assert page_type == "informational"

    def test_a_pricing_guide_is_hybrid(self):
        page_type, _ = classify_page_type(
            "commercial",
            title="How much does a temple visit cost? A pricing guide",
            h1="Understanding ticket prices",
            content="Learn what tickets cost and how to book. Prices explained.",
        )
        assert page_type == "hybrid"

    def test_page_type_is_independent_of_search_intent(self):
        commercial_intent_informational_page, _ = classify_page_type(
            "commercial",
            title="A complete guide to what these products do",
            h1="How it works, explained",
            content="Learn why and how. An overview with examples and definitions.",
        )
        assert commercial_intent_informational_page in {"informational", "hybrid"}


class TestIntentMismatch:
    """§6.4 — the highest-value check in the document."""

    INFORMATIONAL_QUERIES = [
        {"query": "history of temple", "impressions": 5000, "clicks": 50, "position": 9},
        {"query": "what is temple architecture", "impressions": 3000, "clicks": 20, "position": 11},
    ]

    def test_a_transactional_page_attracting_informational_queries_is_p0(self):
        result = detect_intent_mismatch(
            "https://x.test/booking", "transactional", self.INFORMATIONAL_QUERIES
        )
        assert result.has_mismatch and result.severity == "P0"
        assert result.evidence_source == "gsc_queries"

    def test_mismatch_is_detectable_without_any_gsc_data(self):
        """The check must work on a site's first crawl, before Search Console is connected."""
        result = detect_intent_mismatch(
            "https://x.test/booking", "transactional", None,
            title="History of the Temple — Architecture and Facts",
            h1="Temple History",
        )
        assert result.has_mismatch and result.severity == "P0"
        assert result.evidence_source == "page_targeting"

    def test_an_aligned_page_reports_no_mismatch(self):
        result = detect_intent_mismatch(
            "https://x.test/booking", "transactional", None,
            title="Book Tickets Online", h1="Book your visit",
        )
        assert not result.has_mismatch

    def test_gsc_evidence_is_preferred_over_page_copy(self):
        result = detect_intent_mismatch(
            "https://x.test/booking", "transactional", self.INFORMATIONAL_QUERIES,
            title="Book Tickets Online", h1="Book your visit",
        )
        assert result.evidence_source == "gsc_queries"

    def test_no_evidence_at_all_makes_no_claim(self):
        result = detect_intent_mismatch("https://x.test/x", "transactional", None)
        assert not result.has_mismatch
        assert result.evidence_source == "none"

    def test_the_explanation_names_its_evidence_correctly(self):
        by_copy = detect_intent_mismatch(
            "https://x.test/booking", "transactional", None,
            title="History of the Temple", h1="Temple History",
        )
        assert "own title and headings" in by_copy.explanation
        assert "top queries reaching it" not in by_copy.explanation


# ── §5.4: keyword opportunity ───────────────────────────────────────────────


class TestKeywordOpportunity:
    GSC = [
        {"query": "temple darshan booking", "impressions": 48000, "clicks": 1000, "position": 8},
        {"query": "darshan booking", "impressions": 20000, "clicks": 500, "position": 6},
        {"query": "temple history", "impressions": 9000, "clicks": 200, "position": 42},
        {"query": "temple timings", "impressions": 4000, "clicks": 100, "position": 18},
    ]
    PAGE = {
        "title": "Temple Darshan Booking — Book Online",
        "h1": "Book Your Darshan",
        "headings": "Booking steps",
        "content": "Book darshan tickets online. Darshan booking is simple.",
    }

    def test_booking_terms_are_recognised_as_transactional(self):
        """'booking' must match, not just the exact token 'book'."""
        assert _keyword_intent_guess("darshan booking") == "transactional"
        assert _keyword_intent_guess("online booking") == "transactional"
        assert _keyword_intent_guess("temple near me") == "local"
        assert _keyword_intent_guess("best temple guide") == "commercial"
        assert _keyword_intent_guess("how to reach the temple") == "informational"

    def test_the_top_query_is_primary_regardless_of_word_count(self):
        result = build_keyword_tiers(
            "https://x.test/booking", "transactional",
            gsc_queries=self.GSC, page_text=self.PAGE, business_relevance=0.95,
        )
        assert "temple darshan booking" in result.primary

    def test_scores_discriminate_the_way_the_roadmap_expects(self):
        """§5.4: the booking keyword must rank far above 'temple history'."""
        result = build_keyword_tiers(
            "https://x.test/booking", "transactional",
            gsc_queries=self.GSC, page_text=self.PAGE, business_relevance=0.95,
        )
        by_keyword = {k.keyword: k.keyword_opportunity_score for k in result.keywords}
        assert by_keyword["darshan booking"] > by_keyword["temple history"] + 20

    def test_all_six_factors_actually_vary(self):
        """Four of the six used to be constants, which made the formula inert."""
        result = build_keyword_tiers(
            "https://x.test/booking", "transactional",
            gsc_queries=self.GSC, page_text=self.PAGE, business_relevance=0.95,
        )
        for field_name in ("content_relevance_score", "competition_opportunity_score",
                           "intent_match_score", "ranking_opportunity_score"):
            values = {getattr(k, field_name) for k in result.keywords}
            assert len(values) > 1, f"{field_name} is constant across keywords"

    def test_content_relevance_reads_the_actual_page(self):
        on_page = content_relevance("darshan booking", self.PAGE)
        absent = content_relevance("helicopter insurance", self.PAGE)
        assert on_page > absent

    def test_content_relevance_is_neutral_when_the_page_is_unavailable(self):
        assert content_relevance("anything", None) == 0.50

    def test_competition_opportunity_uses_difficulty_when_present(self):
        easy = competition_opportunity(None, 10.0, None)
        hard = competition_opportunity(None, 90.0, None)
        assert easy > hard

    def test_competition_opportunity_falls_back_to_observed_ranking(self):
        ranking = competition_opportunity(5.0, None, None)
        unknown = competition_opportunity(None, None, None)
        assert ranking > unknown

    def test_business_relevance_is_the_pages_value_not_a_copy_of_intent_match(self):
        high = build_keyword_tiers(
            "https://x.test/b", "transactional", gsc_queries=self.GSC,
            page_text=self.PAGE, business_relevance=0.95,
        )
        low = build_keyword_tiers(
            "https://x.test/b", "transactional", gsc_queries=self.GSC,
            page_text=self.PAGE, business_relevance=0.10,
        )
        assert high.page_keyword_opportunity_score > low.page_keyword_opportunity_score


# ── Catalog integrity ───────────────────────────────────────────────────────


class TestCatalogCoversTheRuleRegistry:
    def test_every_registered_rule_has_a_catalog_entry(self):
        from app.services.seo.registry import registry
        import app.services.seo.rules  # noqa: F401  (registers the rules)

        missing = sorted(
            {r.check_type for r in registry.all()} - set(catalog.CATALOG)
        )
        assert not missing, f"check types with no impact catalog entry: {missing}"

    def test_leverage_and_ceiling_are_in_range(self):
        for key, entry in catalog.CATALOG.items():
            assert 0.0 <= entry.search_leverage <= 1.0, key
            assert 0.0 <= entry.activity_leverage <= 1.0, key
            assert 0.0 < entry.ceiling <= 1.0, key
            assert entry.effort in {"low", "medium", "high"}, key
            assert entry.mechanism, key
