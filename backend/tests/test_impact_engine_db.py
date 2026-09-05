"""The impact engine end to end: real pages, real issues, persisted recommendation rows.

The unit tests in ``test_impact_scoring`` prove the formula behaves. These prove the engine
writes what §11.1 says it should, and that the cheap path really is cheap — a site of any size
produces a complete ranked plan without a model call.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models import (
    GA4Metric,
    GSCMetric,
    Page,
    PageIntentProfile,
    RecommendationScore,
    SEOIssue,
    Severity,
    Website,
)
from app.services.impact.engine import score_website_recommendations
from app.utils.url_utils import url_hash


@pytest.fixture
def site(db, member_user):
    website = Website(
        name="Acme", url="https://acme.test", domain="acme.test",
        created_by_id=member_user.id,
    )
    db.add(website)
    db.commit()
    return website


def add_page(db, website, path, **kwargs):
    url = f"https://acme.test{path}"
    page = Page(
        website_id=website.id, url=url, url_hash=url_hash(url), path=path,
        is_active=True, title=kwargs.pop("title", f"Page {path}"),
        h1=kwargs.pop("h1", "Heading"), word_count=kwargs.pop("word_count", 500),
        image_count=3, missing_alt_count=kwargs.pop("missing_alt", 0),
        internal_link_count=5, **kwargs,
    )
    db.add(page)
    db.flush()
    return page


def add_issue(db, page, check_type, severity=Severity.HIGH):
    # Issues are attached without a parent audit row, matching the convention in test_ai.py.
    issue = SEOIssue(
        seo_audit_id=0, page_id=page.id, rule_id=check_type, check_type=check_type,
        severity=severity, title=check_type, description=f"{check_type} is wrong",
        is_resolved=False,
    )
    db.add(issue)
    db.flush()
    return issue


def add_metrics(db, page, *, impressions=0, clicks=0, position=None,
                sessions=0, conversions=0.0, revenue=0.0, engagement=0.5):
    today = date.today() - timedelta(days=1)
    if impressions or clicks:
        db.add(GSCMetric(
            website_id=page.website_id, page_id=page.id, date=today,
            clicks=clicks, impressions=impressions,
            ctr=(clicks / impressions if impressions else 0.0), position=position,
        ))
    if sessions or conversions or revenue:
        db.add(GA4Metric(
            website_id=page.website_id, page_id=page.id, date=today,
            users=sessions, sessions=sessions, engagement_rate=engagement,
            conversions=conversions, revenue=revenue,
        ))
    db.flush()


class TestImpactEnginePersistence:
    def test_it_writes_one_row_per_recommendation_not_per_page(self, db, site):
        page = add_page(db, site, "/pricing")
        add_issue(db, page, "title", Severity.HIGH)
        add_issue(db, page, "image_alt", Severity.LOW)
        add_issue(db, page, "meta_description", Severity.MEDIUM)
        db.commit()

        outcome = score_website_recommendations(db, site)
        db.commit()

        rows = db.query(RecommendationScore).filter_by(page_id=page.id).all()
        assert outcome.recommendations_written == len(rows)
        assert {r.recommendation_type for r in rows} >= {"title", "image_alt", "meta_description"}

    def test_recommendations_on_one_page_receive_different_scores(self, db, site):
        """§4.4 — the whole point of the per-recommendation grain."""
        page = add_page(db, site, "/pricing")
        add_issue(db, page, "title", Severity.HIGH)
        add_issue(db, page, "image_alt", Severity.HIGH)
        add_metrics(db, page, impressions=40000, clicks=400, position=6.0, sessions=2000)
        db.commit()

        score_website_recommendations(db, site)
        db.commit()

        by_type = {
            r.recommendation_type: r
            for r in db.query(RecommendationScore).filter_by(page_id=page.id)
        }
        assert by_type["title"].overall_priority > by_type["image_alt"].overall_priority
        assert by_type["title"].search_impact_score != by_type["title"].user_activity_score

    def test_every_row_carries_the_fields_11_1_requires(self, db, site):
        page = add_page(db, site, "/pricing")
        add_issue(db, page, "title", Severity.HIGH)
        add_metrics(db, page, impressions=10000, clicks=100, position=8.0)
        db.commit()

        score_website_recommendations(db, site)
        db.commit()

        row = db.query(RecommendationScore).filter_by(recommendation_type="title").one()
        assert row.page_id == page.id
        assert row.recommendation_type
        assert row.current_state
        assert row.search_impact_score is not None
        assert row.user_activity_score is not None
        assert row.business_impact_score is not None
        assert row.overall_priority is not None
        assert row.confidence_score is not None
        assert row.reason
        assert row.expected_outcome
        assert row.status == "detected"
        assert row.priority_level in {"P0", "P1", "P2", "P3"}
        assert row.tier in {"rules", "statistical", "ai", "deep_ai"}
        assert "factors" in row.factors

    def test_rescoring_replaces_rather_than_accumulates(self, db, site):
        page = add_page(db, site, "/pricing")
        add_issue(db, page, "title", Severity.HIGH)
        db.commit()

        score_website_recommendations(db, site)
        db.commit()
        first = db.query(RecommendationScore).count()

        score_website_recommendations(db, site)
        db.commit()
        assert db.query(RecommendationScore).count() == first

    def test_a_ctr_gap_becomes_a_recommendation_with_no_rule_failing(self, db, site):
        """A page can under-earn clicks without breaking any SEO rule at all."""
        page = add_page(db, site, "/well-formed")
        add_issue(db, page, "image_alt", Severity.LOW)  # nothing serious
        add_metrics(db, page, impressions=30000, clicks=150, position=4.0)
        db.commit()

        score_website_recommendations(db, site)
        db.commit()

        types = {r.recommendation_type for r in db.query(RecommendationScore)}
        assert "ctr_opportunity" in types

    def test_an_intent_mismatch_becomes_its_own_recommendation(self, db, site):
        page = add_page(db, site, "/booking")
        add_issue(db, page, "title", Severity.MEDIUM)
        db.add(PageIntentProfile(
            page_id=page.id, website_id=site.id, detected_intent="informational",
            business_intent="transactional", intent_mismatch=True, mismatch_severity="P0",
            mismatch_explanation="targets informational keywords", page_type="commercial",
        ))
        db.commit()

        score_website_recommendations(db, site)
        db.commit()

        row = (
            db.query(RecommendationScore)
            .filter_by(recommendation_type="search_intent_mismatch")
            .one()
        )
        assert row.priority_level == "P0"
        assert row.search_intent == "informational"

    def test_business_relevance_lifts_a_revenue_page_above_an_identical_one(self, db, site):
        earner = add_page(db, site, "/earner")
        quiet = add_page(db, site, "/quiet")
        # Identical search profiles; only the commercial outcome differs.
        for page in (earner, quiet):
            add_issue(db, page, "title", Severity.HIGH)
        add_metrics(db, earner, impressions=10000, clicks=100, position=8.0,
                    sessions=500, conversions=50, revenue=25000.0)
        add_metrics(db, quiet, impressions=10000, clicks=100, position=8.0, sessions=500)
        db.commit()

        score_website_recommendations(db, site)
        db.commit()

        scores = {
            r.page_id: r.business_impact_score
            for r in db.query(RecommendationScore).filter_by(recommendation_type="title")
        }
        assert scores[earner.id] > scores[quiet.id]

    def test_scoring_makes_no_model_calls(self, db, site, monkeypatch):
        """§12.3 — the ranked plan is produced entirely from stored data."""
        import app.services.ai.providers as providers

        def explode(*args, **kwargs):
            raise AssertionError("impact scoring must not call a model")

        monkeypatch.setattr(providers, "build_providers", explode, raising=False)

        for index in range(25):
            page = add_page(db, site, f"/p{index}")
            add_issue(db, page, "title", Severity.HIGH)
        db.commit()

        outcome = score_website_recommendations(db, site)
        db.commit()
        assert outcome.pages_scored == 25
        assert outcome.recommendations_written >= 25

    def test_tier_counts_are_reported_for_cost_auditing(self, db, site):
        page = add_page(db, site, "/pricing")
        add_issue(db, page, "title", Severity.HIGH)
        db.commit()

        outcome = score_website_recommendations(db, site)
        assert "ai_calls" in outcome.tier_counts
        assert sum(outcome.priority_counts.values()) == outcome.recommendations_written

    def test_a_page_with_no_issues_produces_no_recommendations(self, db, site):
        add_page(db, site, "/clean")
        db.commit()

        outcome = score_website_recommendations(db, site)
        db.commit()
        assert outcome.recommendations_written == 0
        assert db.query(RecommendationScore).count() == 0
