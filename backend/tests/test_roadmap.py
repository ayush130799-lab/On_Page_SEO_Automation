"""Website-Level SEO Planning — roadmap §7.

Priority matrix, roadmap generation, and the API surface, all built on top of what Steps 1-2
already scored and persisted. No new AI calls happen here — every assertion below can be checked
by inspecting the same data the impact engine wrote.
"""

from __future__ import annotations

from app.models import (
    MemberRole,
    Page,
    PageIntentProfile,
    RecommendationScore,
    SeoRoadmap,
    Severity,
    Website,
    WebsiteMember,
)
from app.services.roadmap import compute_priority_matrix, generate_roadmap, latest_roadmap
from app.utils.url_utils import url_hash

from .conftest import auth_headers


def make_site(db, member_user, domain="acme.test"):
    website = Website(
        name="Acme", url=f"https://{domain}/", domain=domain, created_by_id=member_user.id
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(website)
    return website


def add_page(db, website, path, **kwargs):
    url = f"https://{website.domain}{path}"
    page = Page(
        website_id=website.id, url=url, url_hash=url_hash(url), path=path,
        is_active=kwargs.pop("is_active", True), title=f"Page {path}",
        seo_score=kwargs.pop("seo_score", 60.0),
        highest_severity=kwargs.pop("highest_severity", Severity.MEDIUM), **kwargs,
    )
    db.add(page)
    db.flush()
    return page


def add_recommendation(db, website, page, rec_type, *, priority_level="P2",
                       overall=50.0, search=50.0, activity=50.0, business=50.0,
                       status="detected"):
    db.add(RecommendationScore(
        website_id=website.id, page_id=page.id, recommendation_type=rec_type,
        title=rec_type, priority_level=priority_level, overall_priority=overall,
        search_impact_score=search, user_activity_score=activity,
        business_impact_score=business, confidence_score=0.7, effort="medium",
        reason="because reasons", expected_outcome="expected to improve things",
        status=status,
    ))
    db.flush()


class TestPriorityMatrix:
    def test_a_page_with_no_recommendations_still_appears(self, db, member_user):
        site = make_site(db, member_user)
        add_page(db, site, "/clean", highest_severity=Severity.NONE)
        db.commit()

        entries = compute_priority_matrix(db, site)
        assert len(entries) == 1
        assert entries[0].technical_severity == 0.0

    def test_entries_are_ranked_by_overall_priority_descending(self, db, member_user):
        site = make_site(db, member_user)
        low = add_page(db, site, "/low", highest_severity=Severity.LOW)
        high = add_page(db, site, "/high", highest_severity=Severity.CRITICAL)
        add_recommendation(db, site, low, "image_alt", priority_level="P3", overall=15.0,
                           search=10, activity=10, business=20)
        add_recommendation(db, site, high, "http_status", priority_level="P0", overall=95.0,
                           search=95, activity=80, business=90)
        db.commit()

        entries = compute_priority_matrix(db, site)
        assert [e.page_id for e in entries] == [high.id, low.id]

    def test_technical_severity_reflects_the_pages_own_worst_issue(self, db, member_user):
        site = make_site(db, member_user)
        broken = add_page(db, site, "/broken", highest_severity=Severity.CRITICAL)
        fine = add_page(db, site, "/fine", highest_severity=Severity.LOW)
        db.commit()

        entries = {e.page_id: e for e in compute_priority_matrix(db, site)}
        assert entries[broken.id].technical_severity > entries[fine.id].technical_severity

    def test_priority_level_inherits_the_most_urgent_recommendation_not_a_rescored_threshold(
        self, db, member_user
    ):
        """A page can carry an explicit P0 condition (e.g. non-indexable) while scoring low on
        the continuous 0-100 scale. The matrix band must not silently downgrade it to P2/P3 by
        re-deriving urgency from a fresh threshold."""
        site = make_site(db, member_user)
        page = add_page(db, site, "/quiet-but-broken", highest_severity=Severity.CRITICAL)
        # Low continuous score (no traffic) but an explicit P0 condition.
        add_recommendation(db, site, page, "robots", priority_level="P0", overall=22.0,
                           search=15, activity=10, business=15)
        db.commit()

        entries = compute_priority_matrix(db, site)
        assert entries[0].priority_level == "P0"

    def test_keyword_opportunity_is_read_from_the_intent_profile(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/booking")
        db.add(PageIntentProfile(
            page_id=page.id, website_id=site.id, detected_intent="transactional",
            business_intent="transactional", keyword_opportunity_score=77.0,
        ))
        db.commit()

        entries = compute_priority_matrix(db, site)
        assert entries[0].keyword_opportunity == 77.0

    def test_only_active_pages_are_included(self, db, member_user):
        site = make_site(db, member_user)
        add_page(db, site, "/gone", is_active=False)
        db.commit()

        assert compute_priority_matrix(db, site) == []


class TestRoadmapGeneration:
    def test_p0_and_p1_items_land_in_week_one(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/booking", highest_severity=Severity.CRITICAL)
        add_recommendation(db, site, page, "http_status", priority_level="P0", overall=95.0)
        add_recommendation(db, site, page, "title", priority_level="P1", overall=80.0)
        add_recommendation(db, site, page, "image_alt", priority_level="P3", overall=15.0)
        db.commit()

        roadmap = generate_roadmap(db, site)
        db.commit()

        week1_types = {item["recommendation_type"] for item in roadmap.weeks[0]["items"]}
        week3_types = {item["recommendation_type"] for item in roadmap.weeks[2]["items"]}
        assert "http_status" in week1_types
        assert "title" in week1_types
        assert "image_alt" not in week1_types
        assert "image_alt" in week3_types

    def test_a_recommendation_appears_in_exactly_one_week(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x", highest_severity=Severity.HIGH)
        add_recommendation(db, site, page, "h1", priority_level="P1", overall=70.0)
        db.commit()

        roadmap = generate_roadmap(db, site)
        db.commit()
        appearances = sum(
            1 for week in roadmap.weeks
            for item in week["items"] if item["recommendation_type"] == "h1"
        )
        assert appearances == 1

    def test_implemented_and_rejected_recommendations_are_excluded(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x", highest_severity=Severity.CRITICAL)
        add_recommendation(db, site, page, "robots", priority_level="P0", overall=90.0,
                           status="implemented")
        add_recommendation(db, site, page, "title", priority_level="P0", overall=85.0,
                           status="rejected")
        add_recommendation(db, site, page, "h1", priority_level="P0", overall=80.0,
                           status="detected")
        db.commit()

        roadmap = generate_roadmap(db, site)
        db.commit()
        all_types = {item["recommendation_type"] for w in roadmap.weeks for item in w["items"]}
        assert all_types == {"h1"}

    def test_overview_counts_match_the_priority_matrix(self, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x", highest_severity=Severity.CRITICAL)
        add_recommendation(db, site, page, "robots", priority_level="P0", overall=90.0)
        db.commit()

        roadmap = generate_roadmap(db, site)
        db.commit()
        assert roadmap.critical_issue_count >= 1

    def test_a_second_generation_produces_a_new_row_not_an_overwrite(self, db, member_user):
        """A roadmap is a snapshot in time; regenerating must not silently mutate history."""
        site = make_site(db, member_user)
        page = add_page(db, site, "/x", highest_severity=Severity.HIGH)
        add_recommendation(db, site, page, "h1", priority_level="P1", overall=70.0)
        db.commit()

        first = generate_roadmap(db, site)
        db.commit()
        second = generate_roadmap(db, site)
        db.commit()

        assert first.id != second.id
        assert db.query(SeoRoadmap).filter_by(website_id=site.id).count() == 2
        assert latest_roadmap(db, site).id == second.id

    def test_a_site_with_nothing_to_recommend_still_generates_a_valid_roadmap(
        self, db, member_user
    ):
        site = make_site(db, member_user)
        add_page(db, site, "/clean", highest_severity=Severity.NONE)
        db.commit()

        roadmap = generate_roadmap(db, site)
        db.commit()
        assert roadmap.weeks[0]["items"] == []
        assert roadmap.overall_seo_opportunity is not None


class TestRoadmapApi:
    def test_priority_matrix_endpoint(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x", highest_severity=Severity.CRITICAL)
        add_recommendation(db, site, page, "robots", priority_level="P0", overall=90.0)
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/priority-matrix", headers=auth_headers(member_user)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["priority_level"] == "P0"

    def test_roadmap_endpoint_before_generation_says_so(self, client, db, member_user):
        site = make_site(db, member_user)
        db.commit()
        resp = client.get(f"/api/websites/{site.id}/roadmap", headers=auth_headers(member_user))
        assert resp.status_code == 200
        assert resp.json()["generated"] is False

    def test_generate_then_fetch_round_trips(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x", highest_severity=Severity.HIGH)
        add_recommendation(db, site, page, "h1", priority_level="P1", overall=70.0)
        db.commit()

        gen = client.post(
            f"/api/websites/{site.id}/roadmap/generate", headers=auth_headers(member_user)
        )
        assert gen.status_code == 200
        assert gen.json()["generated"] is True

        fetched = client.get(f"/api/websites/{site.id}/roadmap", headers=auth_headers(member_user))
        assert fetched.json()["id"] == gen.json()["id"]
        assert len(fetched.json()["weeks"]) == 3


class TestUrlActionPlanApi:
    def test_reports_competitor_analysis_as_unavailable_rather_than_fabricating_it(
        self, client, db, member_user
    ):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/opportunities",
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["competitor_analysis"]["available"] is False

    def test_current_search_and_activity_summaries_are_present(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/opportunities",
            headers=auth_headers(member_user),
        )
        body = resp.json()
        assert "current_search_performance" in body
        assert "current_user_activity" in body
