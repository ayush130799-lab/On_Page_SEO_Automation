"""The priority engine.

The headline requirement is asserted directly in
:class:`TestBusinessValueOutranksTechnicalSeverity`: a page with a *better* SEO score but far more
users, conversions and search traffic must rank above a technically worse page nobody visits.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models import (
    GA4Metric,
    GSCMetric,
    MemberRole,
    Page,
    PriorityScore,
    SemrushMetric,
    Setting,
    Severity,
    Website,
    WebsiteMember,
)
from app.services.priority import (
    available_data_sources,
    compute_priorities,
    default_weights,
    ga4_activity_raw,
    gsc_search_raw,
    normalise,
    percentile_ranks,
    redistribute,
    resolve_weights,
    score_website,
    semrush_opportunity_raw,
    seo_severity_raw,
    set_weights,
    severity_band,
)
from app.utils.url_utils import url_hash, url_path

from .conftest import auth_headers

# The engine aggregates metrics over a window ending at the real current date, so fixtures must
# write rows relative to today rather than to a pinned literal.
TODAY = date.today()


@pytest.fixture
def site(db, member_user):
    website = Website(
        name="Acme", url="https://acme.test/", domain="acme.test",
        created_by_id=member_user.id,
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(website)
    return website


def add_page(db, site, path, *, seo_score=80.0, severity=Severity.MEDIUM, issues=3):
    url = f"https://acme.test{path}"
    page = Page(
        website_id=site.id,
        url=url,
        url_hash=url_hash(url),
        path=url_path(url),
        is_active=True,
        seo_score=seo_score,
        highest_severity=severity,
        issue_count=issues,
        status_code=200,
    )
    db.add(page)
    db.flush()
    return page


def add_metrics(db, site, page, *, users=0, sessions=0, conversions=0.0, revenue=0.0,
                clicks=0, impressions=0, position=None, days=14):
    """Spread totals evenly across ``days`` so the window aggregation is genuinely exercised."""
    for offset in range(days):
        day = TODAY - timedelta(days=offset)
        if users or sessions or conversions or revenue:
            db.add(
                GA4Metric(
                    website_id=site.id, page_id=page.id, date=day,
                    users=users // days, sessions=sessions // days,
                    conversions=conversions / days, revenue=revenue / days,
                    engagement_rate=0.6,
                )
            )
        if clicks or impressions:
            db.add(
                GSCMetric(
                    website_id=site.id, page_id=page.id, date=day,
                    clicks=clicks // days, impressions=impressions // days,
                    ctr=(clicks / impressions) if impressions else 0.0,
                    position=position,
                )
            )
    db.commit()


# ── Weights ─────────────────────────────────────────────────────────────────


class TestWeights:
    def test_defaults_match_the_specification(self):
        assert default_weights() == {
            "seo_severity": 0.40,
            "ga4_activity": 0.30,
            "gsc_search": 0.20,
            "semrush_opportunity": 0.10,
        }
        assert sum(default_weights().values()) == pytest.approx(1.0)

    def test_weights_are_never_hard_coded_in_the_engine(self):
        """Every weight must be reachable through configuration, not literals in the code."""
        import inspect

        from app.services.priority import components, engine

        for module in (engine, components):
            source = inspect.getsource(module)
            for literal in ("0.40", "0.30", "0.20", "0.10"):
                assert f"= {literal}" not in source, (
                    f"{module.__name__} appears to hard-code the weight {literal}"
                )

    def test_normalisation_rescales_to_one(self):
        assert normalise({"a": 2, "b": 2}) == {"a": 0.5, "b": 0.5}
        assert sum(normalise({"a": 7, "b": 3, "c": 1}).values()) == pytest.approx(1.0)

    def test_all_zero_weights_fall_back_to_the_defaults(self):
        assert normalise({k: 0.0 for k in default_weights()}) == default_weights()

    def test_global_override_is_applied(self, db, site):
        set_weights(db, {"seo_severity": 0.7, "ga4_activity": 0.1,
                         "gsc_search": 0.1, "semrush_opportunity": 0.1})
        assert resolve_weights(db, None)["seo_severity"] == pytest.approx(0.7)

    def test_website_override_beats_the_global_one(self, db, site):
        set_weights(db, {"seo_severity": 0.7, "ga4_activity": 0.1,
                         "gsc_search": 0.1, "semrush_opportunity": 0.1})
        set_weights(db, {"ga4_activity": 0.9}, website_id=site.id)

        website_weights = resolve_weights(db, site.id)
        assert website_weights["ga4_activity"] > website_weights["seo_severity"]
        # The global setting is untouched.
        assert resolve_weights(db, None)["seo_severity"] == pytest.approx(0.7)

    def test_a_partial_override_keeps_the_other_components(self, db, site):
        saved = set_weights(db, {"ga4_activity": 0.5}, website_id=site.id)
        assert set(saved) == set(default_weights())
        assert sum(saved.values()) == pytest.approx(1.0)

    def test_setting_no_components_is_rejected(self, db):
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            set_weights(db, {})

    def test_a_missing_provider_redistributes_rather_than_zero_filling(self):
        without_ga4 = redistribute(default_weights(), {"seo", "gsc", "semrush"})
        assert without_ga4["ga4_activity"] == 0.0
        assert sum(without_ga4.values()) == pytest.approx(1.0)
        # The remaining weights keep their ratio to each other: 40:20:10 -> 4:2:1.
        assert without_ga4["seo_severity"] == pytest.approx(4 / 7, abs=1e-4)
        assert without_ga4["gsc_search"] == pytest.approx(2 / 7, abs=1e-4)

    def test_with_no_integrations_priority_is_pure_technical_severity(self):
        assert redistribute(default_weights(), {"seo"})["seo_severity"] == 1.0


# ── Normalisation primitives ────────────────────────────────────────────────


class TestNormalisation:
    def test_percentile_ranks_span_zero_to_one(self):
        assert percentile_ranks([10, 20, 30, 40, 50]) == [0.0, 0.25, 0.5, 0.75, 1.0]

    def test_ties_share_a_rank(self):
        assert percentile_ranks([5, 5, 9]) == [0.0, 0.0, 1.0]

    def test_a_uniform_distribution_contributes_nothing(self):
        """If every page is identical on a signal, that signal cannot discriminate."""
        assert percentile_ranks([7, 7, 7, 7]) == [0.0, 0.0, 0.0, 0.0]

    def test_empty_and_single_inputs_are_safe(self):
        assert percentile_ranks([]) == []
        assert percentile_ranks([42]) == [0.0]

    def test_outliers_do_not_flatten_the_middle(self):
        """Rank-based normalisation is immune to the long tail that raw counts suffer from."""
        ranks = percentile_ranks([1, 2, 3, 4, 1_000_000])
        assert ranks[3] == 0.75  # unaffected by the outlier


# ── Component behaviour ─────────────────────────────────────────────────────


class TestComponents:
    def test_severity_dominates_the_seo_component(self):
        critical = seo_severity_raw(
            type("P", (), {"highest_severity": "CRITICAL", "seo_score": 85.0, "issue_count": 1})
        )
        low = seo_severity_raw(
            type("P", (), {"highest_severity": "LOW", "seo_score": 85.0, "issue_count": 1})
        )
        assert critical > low

    def test_a_lower_seo_score_raises_the_severity_component(self):
        worse = seo_severity_raw(
            type("P", (), {"highest_severity": "HIGH", "seo_score": 30.0, "issue_count": 5})
        )
        better = seo_severity_raw(
            type("P", (), {"highest_severity": "HIGH", "seo_score": 85.0, "issue_count": 5})
        )
        assert worse > better

    def test_a_perfect_page_scores_zero_severity(self):
        assert seo_severity_raw(
            type("P", (), {"highest_severity": "NONE", "seo_score": 100.0, "issue_count": 0})
        ) == 0.0

    def test_conversions_outweigh_raw_users_in_the_ga4_component(self):
        converting = ga4_activity_raw({"users": 500, "sessions": 600, "conversions": 60,
                                       "revenue": 30000})
        browsing = ga4_activity_raw({"users": 5000, "sessions": 6000, "conversions": 0,
                                     "revenue": 0})
        assert converting > browsing

    def test_striking_distance_positions_score_highest(self):
        base = {"clicks": 100, "impressions": 5000, "ctr": 0.02}
        page_two = gsc_search_raw({**base, "position": 12})
        already_first = gsc_search_raw({**base, "position": 1})
        far_back = gsc_search_raw({**base, "position": 75})
        assert page_two > already_first
        assert page_two > far_back

    def test_a_ctr_below_the_expected_curve_adds_signal(self):
        under = gsc_search_raw({"clicks": 20, "impressions": 10000, "ctr": 0.002, "position": 3})
        at_par = gsc_search_raw({"clicks": 1100, "impressions": 10000, "ctr": 0.11, "position": 3})
        # The under-performer gets the CTR-gap bonus the well-performing page does not.
        assert under > 0
        assert at_par > under  # clicks still dominate, as they should

    def test_semrush_opportunity_rewards_striking_distance_volume(self):
        with_opportunity = semrush_opportunity_raw(
            {"organic_keywords": 40, "organic_traffic": 500, "opportunity_volume": 50000,
             "backlinks": 10}
        )
        without = semrush_opportunity_raw(
            {"organic_keywords": 40, "organic_traffic": 500, "opportunity_volume": 0,
             "backlinks": 10}
        )
        assert with_opportunity > without

    def test_missing_metrics_are_zero_not_an_error(self):
        assert ga4_activity_raw({}) == 0.0
        assert gsc_search_raw({}) == 0.0
        assert semrush_opportunity_raw({}) == 0.0


# ── The core requirement ────────────────────────────────────────────────────


class TestBusinessValueOutranksTechnicalSeverity:
    """The behaviour the whole platform exists to produce."""

    @pytest.fixture
    def scored(self, db, site):
        # Technically healthier (SEO 82) but carries the business.
        money_page = add_page(
            db, site, "/pricing", seo_score=82.0, severity=Severity.MEDIUM, issues=2
        )
        add_metrics(
            db, site, money_page,
            users=42000, sessions=51000, conversions=980, revenue=310000,
            clicks=9800, impressions=180000, position=6.4,
        )

        # Technically far worse (SEO 41) but essentially nobody sees it.
        forgotten_page = add_page(
            db, site, "/legacy/2019-archive", seo_score=41.0, severity=Severity.HIGH, issues=9
        )
        add_metrics(
            db, site, forgotten_page, users=3, sessions=3, clicks=0, impressions=12,
            position=61.0,
        )

        # Filler so the distribution has enough points to rank against.
        for index in range(10):
            filler = add_page(
                db, site, f"/blog/post-{index}",
                seo_score=70.0 + index, severity=Severity.MEDIUM, issues=3,
            )
            add_metrics(
                db, site, filler, users=200 + index * 30, sessions=250,
                clicks=20 + index, impressions=800, position=15.0,
            )

        db.commit()
        result = compute_priorities(db, site, window_days=28)
        by_path = {p.url.rsplit("/", 1)[-1] or "root": p for p in result.priorities}
        return result, money_page, forgotten_page, by_path

    def test_the_high_value_page_ranks_above_the_broken_low_traffic_one(self, scored):
        result, money_page, forgotten_page, _ = scored
        ranks = {p.page_id: p.rank for p in result.priorities}
        scores = {p.page_id: p.score for p in result.priorities}

        assert ranks[money_page.id] < ranks[forgotten_page.id], (
            "A page with far more users, conversions and search traffic must be prioritised "
            "over a technically worse page with no audience."
        )
        assert scores[money_page.id] > scores[forgotten_page.id]

    def test_the_seo_score_ordering_is_the_opposite(self, db, scored):
        """Confirms the two scores really are independent, not the same number twice."""
        _, money_page, forgotten_page, _ = scored
        assert money_page.seo_score > forgotten_page.seo_score

    def test_the_high_value_page_lands_in_the_top_band(self, scored):
        result, money_page, _, _ = scored
        priority = next(p for p in result.priorities if p.page_id == money_page.id)
        assert priority.band in ("P0", "P1")

    def test_the_breakdown_explains_why(self, scored):
        result, money_page, forgotten_page, _ = scored
        money = next(p for p in result.priorities if p.page_id == money_page.id)
        forgotten = next(p for p in result.priorities if p.page_id == forgotten_page.id)

        # Business signals carry the money page; technical severity carries the other.
        assert money.components["ga4_activity"] > forgotten.components["ga4_activity"]
        assert money.components["gsc_search"] > forgotten.components["gsc_search"]
        assert forgotten.components["seo_severity"] > money.components["seo_severity"]

    def test_removing_the_business_weights_flips_the_order(self, db, site, scored):
        """With severity weighted at 100%, the broken page correctly wins — proving the weights
        are what drives the outcome, not an accident of the data."""
        _, money_page, forgotten_page, _ = scored

        severity_only = compute_priorities(
            db, site,
            weights={"seo_severity": 1.0, "ga4_activity": 0.0,
                     "gsc_search": 0.0, "semrush_opportunity": 0.0},
        )
        ranks = {p.page_id: p.rank for p in severity_only.priorities}
        assert ranks[forgotten_page.id] < ranks[money_page.id]


# ── Engine mechanics ────────────────────────────────────────────────────────


class TestEngine:
    def test_scores_are_bounded_and_ranked(self, db, site):
        for index in range(12):
            page = add_page(db, site, f"/p{index}", seo_score=50.0 + index * 3)
            add_metrics(db, site, page, users=index * 100, clicks=index * 10,
                        impressions=index * 200, position=10.0)
        db.commit()

        result = compute_priorities(db, site)
        assert result.pages_scored == 12
        assert all(0 <= p.score <= 100 for p in result.priorities)
        assert [p.rank for p in result.priorities] == list(range(1, 13))
        scores = [p.score for p in result.priorities]
        assert scores == sorted(scores, reverse=True)

    def test_data_sources_reflect_stored_rows_not_connection_status(self, db, site):
        page = add_page(db, site, "/a")
        assert available_data_sources(db, site.id) == {"seo"}

        add_metrics(db, site, page, clicks=10, impressions=100, position=5.0)
        assert available_data_sources(db, site.id) == {"seo", "gsc"}

        db.add(SemrushMetric(website_id=site.id, page_id=page.id, date=TODAY,
                             organic_keywords=5))
        db.commit()
        assert available_data_sources(db, site.id) == {"seo", "gsc", "semrush"}

    def test_a_site_with_no_metrics_still_ranks_by_severity(self, db, site):
        broken = add_page(db, site, "/broken", seo_score=20.0, severity=Severity.CRITICAL,
                          issues=10)
        healthy = add_page(db, site, "/healthy", seo_score=99.0, severity=Severity.NONE,
                           issues=0)
        db.commit()

        result = compute_priorities(db, site)
        assert result.weights["seo_severity"] == 1.0
        ranks = {p.page_id: p.rank for p in result.priorities}
        assert ranks[broken.id] < ranks[healthy.id]

    def test_an_empty_website_scores_without_error(self, db, site):
        result = compute_priorities(db, site)
        assert result.pages_scored == 0
        assert result.priorities == []

    def test_inactive_pages_are_excluded(self, db, site):
        add_page(db, site, "/live")
        stale = add_page(db, site, "/removed")
        stale.is_active = False
        db.commit()

        assert compute_priorities(db, site).pages_scored == 1

    def test_persisting_writes_scores_and_the_explanation(self, db, site):
        page = add_page(db, site, "/a", seo_score=60.0)
        add_metrics(db, site, page, users=1000, clicks=100, impressions=2000, position=8.0)
        for index in range(9):
            other = add_page(db, site, f"/b{index}", seo_score=70.0 + index)
            add_metrics(db, site, other, users=index * 10)
        db.commit()

        result = score_website(db, site)
        assert result.pages_scored == 10

        db.expire_all()
        stored = db.query(PriorityScore).filter(PriorityScore.page_id == page.id).one()
        assert stored.score == pytest.approx(db.get(Page, page.id).priority_score)
        assert stored.band == db.get(Page, page.id).priority_band
        assert stored.rank is not None
        assert set(stored.weights) == set(default_weights())
        assert "raw" in stored.breakdown and "metrics" in stored.breakdown
        assert "seo" in stored.data_sources

    def test_the_weight_vector_is_stored_so_old_scores_stay_explainable(self, db, site):
        add_page(db, site, "/a")
        db.commit()
        score_website(db, site)

        original = db.query(PriorityScore).one().weights
        set_weights(db, {"seo_severity": 0.9}, website_id=site.id)

        # Retuning the weights must not rewrite history.
        assert db.query(PriorityScore).one().weights == original

    def test_rescoring_refreshes_the_page_snapshot(self, db, site):
        page = add_page(db, site, "/a", seo_score=90.0, severity=Severity.LOW)
        for index in range(9):
            add_page(db, site, f"/f{index}", seo_score=50.0, severity=Severity.CRITICAL)
        db.commit()

        score_website(db, site)
        db.expire_all()
        first = db.get(Page, page.id).priority_score

        page = db.get(Page, page.id)
        page.seo_score = 10.0
        page.highest_severity = Severity.CRITICAL
        page.issue_count = 15
        db.commit()

        score_website(db, site)
        db.expire_all()
        assert db.get(Page, page.id).priority_score > first

    def test_website_summary_counts_high_priority_pages(self, db, site):
        for index in range(20):
            page = add_page(db, site, f"/p{index}", seo_score=40.0 + index * 2)
            add_metrics(db, site, page, users=index * 50, clicks=index * 5,
                        impressions=index * 100, position=10.0)
        db.commit()

        score_website(db, site)
        db.refresh(site)
        assert site.high_priority_page_count > 0
        assert site.last_scored_at is not None


# ── Banding ─────────────────────────────────────────────────────────────────


class TestBanding:
    def test_bands_are_relative_to_the_sites_own_distribution(self):
        distribution = [float(i) for i in range(100)]
        assert severity_band(99.0, distribution) == "P0"
        assert severity_band(85.0, distribution) == "P1"
        assert severity_band(60.0, distribution) == "P2"
        assert severity_band(10.0, distribution) == "P3"

    def test_small_sites_use_absolute_bands(self):
        """Percentiles over five pages would mislabel a healthy site as one-fifth critical."""
        small = [80.0, 40.0, 10.0]
        assert severity_band(80.0, small) == "P0"
        assert severity_band(10.0, small) == "P3"

    def test_an_empty_distribution_is_safe(self):
        assert severity_band(50.0, []) == "P3"


# ── API ─────────────────────────────────────────────────────────────────────


class TestPriorityApi:
    def test_scoring_endpoint_returns_the_top_pages(self, client, db, site, member_user):
        for index in range(10):
            page = add_page(db, site, f"/p{index}", seo_score=50.0 + index)
            add_metrics(db, site, page, users=index * 100, clicks=index * 10,
                        impressions=index * 100, position=9.0)
        db.commit()

        response = client.post(
            f"/api/websites/{site.id}/priority/score", headers=auth_headers(member_user)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["pages_scored"] == 10
        assert body["top_pages"][0]["rank"] == 1
        assert set(body["top_pages"][0]["components"]) == set(default_weights())

    def test_preview_does_not_persist(self, client, db, site, member_user):
        add_page(db, site, "/a")
        db.commit()

        response = client.get(
            f"/api/websites/{site.id}/priority/preview?seo_severity=1",
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        assert response.json()["weights"]["seo_severity"] == pytest.approx(1.0, abs=0.01)
        assert db.query(PriorityScore).count() == 0
        assert db.query(Setting).count() == 0

    def test_weights_can_be_read_and_updated_per_website(self, client, site, member_user):
        headers = auth_headers(member_user)
        assert client.get(
            f"/api/websites/{site.id}/priority/weights", headers=headers
        ).json()["weights"]["seo_severity"] == pytest.approx(0.4)

        updated = client.put(
            f"/api/websites/{site.id}/priority/weights",
            json={"ga4_activity": 0.6},
            headers=headers,
        )
        assert updated.status_code == 200
        assert sum(updated.json()["weights"].values()) == pytest.approx(1.0)

    def test_effective_weights_show_the_redistribution(self, client, db, site, member_user):
        body = client.get(
            f"/api/websites/{site.id}/priority/weights", headers=auth_headers(member_user)
        ).json()
        # No metrics stored yet, so only the SEO signal survives.
        assert body["effective_weights"]["seo_severity"] == 1.0
        assert body["data_sources"] == ["seo"]

    def test_global_weight_settings_require_an_administrator(
        self, client, member_user, admin_user
    ):
        assert client.get(
            "/api/settings/priority/weights", headers=auth_headers(member_user)
        ).status_code == 403
        assert client.get(
            "/api/settings/priority/weights", headers=auth_headers(admin_user)
        ).status_code == 200

    def test_settings_endpoint_lists_defaults_and_overrides(self, client, db, site, admin_user):
        set_weights(db, {"ga4_activity": 0.5}, website_id=site.id)
        body = client.get("/api/settings", headers=auth_headers(admin_user)).json()
        assert body["defaults"]["priority_weights"]["seo_severity"] == 0.4
        assert any(o["key"] == "priority_weights" for o in body["overrides"])

    def test_out_of_range_weights_are_rejected(self, client, site, member_user):
        response = client.put(
            f"/api/websites/{site.id}/priority/weights",
            json={"ga4_activity": 5.0},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 422

    def test_pages_are_listed_in_priority_order_by_default(
        self, client, db, site, member_user
    ):
        for index in range(10):
            page = add_page(db, site, f"/p{index}", seo_score=50.0 + index)
            add_metrics(db, site, page, users=index * 100, clicks=index * 10,
                        impressions=index * 100, position=9.0)
        db.commit()
        score_website(db, site)

        items = client.get(
            f"/api/websites/{site.id}/pages", headers=auth_headers(member_user)
        ).json()["items"]
        scores = [i["priority_score"] for i in items]
        assert scores == sorted(scores, reverse=True)
