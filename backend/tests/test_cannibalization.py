"""Keyword cannibalization detection (roadmap §2.1's "possible keyword cannibalization").

Covers the detection function directly (severity ladder, canonical-page selection, tier scoping)
and both API surfaces: the site-wide ``/intent/cannibalization`` list and the per-page
``cannibalization`` field returned by ``/pages/{page_id}/intent``.
"""

from __future__ import annotations

from app.models.intent import KeywordOpportunity, PageIntentProfile
from app.services.intent.cannibalization import detect_cannibalization

from .conftest import auth_headers
from .test_keyword_intelligence import add_page, make_site


def add_profile_with_keywords(db, website, page, keywords: list[dict]):
    """keywords: list of dicts with keys keyword, tier, score, position=None, impressions=None,
    source='gsc'."""
    profile = PageIntentProfile(
        page_id=page.id, website_id=website.id, detected_intent="informational",
        business_intent="informational",
    )
    db.add(profile)
    db.flush()
    for entry in keywords:
        db.add(KeywordOpportunity(
            intent_profile_id=profile.id, page_id=page.id, website_id=website.id,
            keyword=entry["keyword"], keyword_tier=entry.get("tier", "primary"),
            keyword_opportunity_score=entry.get("score", 50.0),
            current_position=entry.get("position"),
            current_impressions=entry.get("impressions"),
            demand_score=0.7, ranking_opportunity_score=0.6, intent_match_score=0.8,
            business_relevance_score=0.5, content_relevance_score=0.6,
            competition_opportunity_score=0.6, source=entry.get("source", "gsc"),
        ))
    db.flush()
    return profile


class TestSeverityLadder:
    def test_both_pages_ranking_top_10_is_p0(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "temple booking", "tier": "primary", "score": 80.0, "position": 4.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "temple booking", "tier": "primary", "score": 60.0, "position": 8.0}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert len(groups) == 1
        assert groups[0].severity == "P0"

    def test_both_ranking_but_not_both_top_10_is_p1(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "temple booking", "tier": "primary", "score": 80.0, "position": 4.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "temple booking", "tier": "primary", "score": 60.0, "position": 35.0}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert groups[0].severity == "P1"

    def test_only_one_page_ranking_is_p2(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "temple booking", "tier": "primary", "score": 80.0, "position": 4.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "temple booking", "tier": "primary", "score": 60.0, "position": None}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert groups[0].severity == "P2"

    def test_neither_page_ranking_is_p3(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "temple booking", "tier": "primary", "score": 80.0, "source": "ai"}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "temple booking", "tier": "primary", "score": 55.0, "source": "ai"}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert groups[0].severity == "P3"


class TestCanonicalPageSelection:
    def test_highest_opportunity_score_wins_as_canonical(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/weak")
        p2 = add_page(db, site, "/strong")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "temple booking", "tier": "primary", "score": 40.0, "position": 15.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "temple booking", "tier": "primary", "score": 85.0, "position": 6.0}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert groups[0].recommended_canonical_page_id == p2.id
        assert groups[0].recommended_canonical_url == p2.url
        assert p2.url in groups[0].explanation
        assert p1.url in groups[0].explanation

    def test_tied_score_breaks_on_better_position(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/deep")
        p2 = add_page(db, site, "/shallow")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "temple booking", "tier": "primary", "score": 70.0, "position": 25.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "temple booking", "tier": "primary", "score": 70.0, "position": 3.0}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert groups[0].recommended_canonical_page_id == p2.id


class TestTierScoping:
    def test_long_tail_overlap_is_not_flagged_by_default(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "how to book a temple visit", "tier": "long_tail", "score": 40.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "how to book a temple visit", "tier": "long_tail", "score": 45.0}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert groups == []

    def test_a_keyword_targeted_by_only_one_page_is_not_flagged(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "unique term", "tier": "primary", "score": 70.0}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id)
        assert groups == []

    def test_tier_filter_restricts_to_secondary_only(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "primary clash", "tier": "primary", "score": 80.0},
            {"keyword": "secondary clash", "tier": "secondary", "score": 50.0},
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "primary clash", "tier": "primary", "score": 60.0},
            {"keyword": "secondary clash", "tier": "secondary", "score": 45.0},
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id, tier="secondary")
        assert len(groups) == 1
        assert groups[0].keyword == "secondary clash"

    def test_a_site_with_no_overlap_returns_no_groups(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [{"keyword": "alpha", "tier": "primary", "score": 70.0}])
        add_profile_with_keywords(db, site, p2, [{"keyword": "beta", "tier": "primary", "score": 60.0}])
        db.commit()

        assert detect_cannibalization(db, site.id) == []


class TestSeverityFilter:
    def test_severity_filter_excludes_non_matching_groups(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        p3 = add_page(db, site, "/c")
        p4 = add_page(db, site, "/d")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "urgent term", "tier": "primary", "score": 80.0, "position": 3.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "urgent term", "tier": "primary", "score": 60.0, "position": 5.0}
        ])
        add_profile_with_keywords(db, site, p3, [
            {"keyword": "quiet term", "tier": "primary", "score": 40.0, "source": "ai"}
        ])
        add_profile_with_keywords(db, site, p4, [
            {"keyword": "quiet term", "tier": "primary", "score": 35.0, "source": "ai"}
        ])
        db.commit()

        p0_groups = detect_cannibalization(db, site.id, severity="P0")
        assert len(p0_groups) == 1
        assert p0_groups[0].keyword == "urgent term"

        p3_groups = detect_cannibalization(db, site.id, severity="P3")
        assert len(p3_groups) == 1
        assert p3_groups[0].keyword == "quiet term"


class TestPageScoping:
    def test_page_ids_filter_still_returns_the_full_competing_group(self, db, member_user):
        """Scoping to one page must not hide the *other* pages competing with it — otherwise a
        single-page view would show a "group" of size one and cannibalization would vanish."""
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        p3 = add_page(db, site, "/c")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "shared", "tier": "primary", "score": 80.0, "position": 4.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "shared", "tier": "primary", "score": 60.0, "position": 6.0}
        ])
        add_profile_with_keywords(db, site, p3, [
            {"keyword": "unrelated", "tier": "primary", "score": 50.0}
        ])
        db.commit()

        groups = detect_cannibalization(db, site.id, page_ids=[p1.id])
        assert len(groups) == 1
        assert {p.page_id for p in groups[0].pages} == {p1.id, p2.id}

    def test_page_with_no_collision_returns_no_groups(self, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [{"keyword": "alpha", "tier": "primary", "score": 70.0}])
        add_profile_with_keywords(db, site, p2, [{"keyword": "beta", "tier": "primary", "score": 60.0}])
        db.commit()

        assert detect_cannibalization(db, site.id, page_ids=[p1.id]) == []


class TestCannibalizationApi:
    def test_site_wide_endpoint_returns_ranked_groups(self, client, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "shared term", "tier": "primary", "score": 80.0, "position": 4.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "shared term", "tier": "primary", "score": 60.0, "position": 7.0}
        ])
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/intent/cannibalization", headers=auth_headers(member_user)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["keyword"] == "shared term"
        assert item["severity"] == "P0"
        assert item["recommended_canonical_page_id"] == p1.id
        assert {p["page_id"] for p in item["pages"]} == {p1.id, p2.id}

    def test_site_wide_endpoint_supports_severity_and_tier_filters(self, client, db, member_user):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "quiet term", "tier": "secondary", "score": 40.0, "source": "ai"}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "quiet term", "tier": "secondary", "score": 35.0, "source": "ai"}
        ])
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/intent/cannibalization?tier=secondary&severity=P3",
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        resp_wrong_tier = client.get(
            f"/api/websites/{site.id}/intent/cannibalization?tier=primary",
            headers=auth_headers(member_user),
        )
        assert resp_wrong_tier.json()["total"] == 0

    def test_a_site_with_no_cannibalization_returns_an_empty_list_not_an_error(
        self, client, db, member_user
    ):
        site = make_site(db, member_user)
        db.commit()
        resp = client.get(
            f"/api/websites/{site.id}/intent/cannibalization", headers=auth_headers(member_user)
        )
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "limit": 50, "offset": 0, "items": []}

    def test_page_intent_endpoint_surfaces_this_pages_cannibalization(
        self, client, db, member_user
    ):
        site = make_site(db, member_user)
        p1 = add_page(db, site, "/a")
        p2 = add_page(db, site, "/b")
        p3 = add_page(db, site, "/c")
        add_profile_with_keywords(db, site, p1, [
            {"keyword": "shared term", "tier": "primary", "score": 80.0, "position": 4.0}
        ])
        add_profile_with_keywords(db, site, p2, [
            {"keyword": "shared term", "tier": "primary", "score": 60.0, "position": 7.0}
        ])
        add_profile_with_keywords(db, site, p3, [
            {"keyword": "unrelated term", "tier": "primary", "score": 50.0}
        ])
        db.commit()

        resp = client.get(
            f"/api/websites/{site.id}/pages/{p1.id}/intent", headers=auth_headers(member_user)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["cannibalization"]) == 1
        assert body["cannibalization"][0]["keyword"] == "shared term"
        assert {p["page_id"] for p in body["cannibalization"][0]["pages"]} == {p1.id, p2.id}

        # The unrelated page's own intent view must not show a phantom cannibalization entry.
        resp3 = client.get(
            f"/api/websites/{site.id}/pages/{p3.id}/intent", headers=auth_headers(member_user)
        )
        assert resp3.json()["cannibalization"] == []
