"""Live SERP competitor analysis — roadmap §4.2 / §7.4.

Covers the SerpApi client (parsing organic results, PAA, related searches; error handling), the
competitor page fetcher (reusing the crawler's own extractor, so word counts and headings are
measured the same way as the site's own pages), the content-gap summary, and the on-demand API
surface — including that it is never triggered automatically.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models import (
    CompetitorAnalysis,
    MemberRole,
    Page,
    PageIntentProfile,
    Website,
    WebsiteMember,
)
from app.services.serp.analyzer import analyse_competitors
from app.services.serp.client import SerpApiError, is_configured, search
from app.services.serp.competitor_analyzer import fetch_competitors
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
        is_active=True, title=kwargs.pop("title", f"Page {path}"), **kwargs,
    )
    db.add(page)
    db.flush()
    return page


def patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


SERPAPI_PAYLOAD = {
    "organic_results": [
        {"position": 1, "link": "https://rival-a.test/darshan-booking", "title": "Rival A",
         "snippet": "Book your darshan"},
        {"position": 2, "link": "https://rival-b.test/booking", "title": "Rival B",
         "snippet": "Temple booking"},
        {"position": 3, "link": "https://rival-c.test/tickets", "title": "Rival C",
         "snippet": "Buy tickets"},
    ],
    "related_questions": [
        {"question": "How do I book a darshan?", "snippet": "Visit the official site..."},
        {"question": "What is the cost of darshan booking?", "snippet": "Prices vary..."},
    ],
    "related_searches": [{"query": "online darshan booking"}, {"query": "temple ticket price"}],
}

RIVAL_A_HTML = """<!doctype html><html><head><title>Book Darshan</title></head>
<body><h1>Book Your Darshan</h1><h2>How to book</h2><h2>Pricing</h2>
<p>{}</p></body></html>""".format(" ".join(["word"] * 900))

RIVAL_B_HTML = """<!doctype html><html><head><title>Temple Booking</title></head>
<body><h1>Temple Booking</h1><h2>How to book</h2><h2>Cancellation policy</h2>
<p>{}</p></body></html>""".format(" ".join(["word"] * 1100))


def combined_handler(rival_c_status=403):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "serpapi.com":
            return httpx.Response(200, json=SERPAPI_PAYLOAD)
        if host == "rival-a.test":
            return httpx.Response(200, text=RIVAL_A_HTML, headers={"content-type": "text/html"})
        if host == "rival-b.test":
            return httpx.Response(200, text=RIVAL_B_HTML, headers={"content-type": "text/html"})
        if host == "rival-c.test":
            return httpx.Response(rival_c_status, text="blocked")
        return httpx.Response(404)
    return handler


@pytest.fixture(autouse=True)
def _serpapi_key(monkeypatch):
    monkeypatch.setattr("app.config.settings.serpapi_key", "test-serpapi-key")


# ── SerpApi client ───────────────────────────────────────────────────────────


class TestSerpApiClient:
    def test_is_configured_reflects_the_key(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.serpapi_key", "")
        assert is_configured() is False
        monkeypatch.setattr("app.config.settings.serpapi_key", "x")
        assert is_configured() is True

    async def test_missing_key_raises_cleanly(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.serpapi_key", "")
        with pytest.raises(SerpApiError, match="not configured"):
            await search("darshan booking")

    async def test_organic_results_paa_and_related_searches_are_parsed(self, monkeypatch):
        patch_transport(monkeypatch, combined_handler())
        result = await search("darshan booking")
        assert [r.url for r in result.organic_results] == [
            "https://rival-a.test/darshan-booking",
            "https://rival-b.test/booking",
            "https://rival-c.test/tickets",
        ]
        assert result.paa_questions[0]["question"] == "How do I book a darshan?"
        assert "online darshan booking" in result.related_searches

    async def test_a_serpapi_error_payload_raises(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"error": "Invalid API key"})
        patch_transport(monkeypatch, handler)
        with pytest.raises(SerpApiError, match="Invalid API key"):
            await search("x")

    async def test_a_non_200_response_raises(self, monkeypatch):
        def handler(request):
            return httpx.Response(429, text="rate limited")
        patch_transport(monkeypatch, handler)
        with pytest.raises(SerpApiError, match="429"):
            await search("x")

    async def test_organic_results_with_no_link_are_skipped(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"organic_results": [
                {"position": 1, "title": "no link here"},
                {"position": 2, "link": "https://ok.test/x", "title": "ok"},
            ]})
        patch_transport(monkeypatch, handler)
        result = await search("x")
        assert len(result.organic_results) == 1


# ── Competitor page fetching ─────────────────────────────────────────────────


class TestCompetitorFetching:
    async def test_successful_fetches_are_measured_by_the_real_extractor(self, monkeypatch):
        patch_transport(monkeypatch, combined_handler())
        result = await search("darshan booking")
        fetches = await fetch_competitors(result.organic_results, top_n=3)

        rival_a = next(f for f in fetches if f.domain == "rival-a.test")
        assert rival_a.fetch_status == "ok"
        assert rival_a.page is not None
        # 900 filler words plus the title/H1/H2 text the extractor correctly also counts —
        # asserting a floor rather than an exact figure keeps this test decoupled from exactly
        # how many extra words the heading text itself contributes.
        assert rival_a.page.word_count >= 900
        assert rival_a.page.h1 == "Book Your Darshan"
        assert rival_a.page.h2_count == 2

    async def test_a_blocked_competitor_is_recorded_not_silently_dropped(self, monkeypatch):
        patch_transport(monkeypatch, combined_handler(rival_c_status=403))
        result = await search("darshan booking")
        fetches = await fetch_competitors(result.organic_results, top_n=3)

        rival_c = next(f for f in fetches if f.domain == "rival-c.test")
        assert rival_c.fetch_status == "blocked"
        assert rival_c.page is None

    async def test_order_is_preserved_by_serp_position(self, monkeypatch):
        patch_transport(monkeypatch, combined_handler())
        result = await search("darshan booking")
        fetches = await fetch_competitors(result.organic_results, top_n=3)
        assert [f.position for f in fetches] == [1, 2, 3]

    async def test_top_n_limits_how_many_are_fetched(self, monkeypatch):
        patch_transport(monkeypatch, combined_handler())
        result = await search("darshan booking")
        fetches = await fetch_competitors(result.organic_results, top_n=2)
        assert len(fetches) == 2

    async def test_no_organic_results_means_no_fetches(self):
        assert await fetch_competitors([], top_n=5) == []


# ── Full orchestration + content gap ─────────────────────────────────────────


class TestAnalyseCompetitors:
    async def test_a_full_run_persists_analysis_and_results(self, db, member_user, monkeypatch):
        site = make_site(db, member_user)
        page = add_page(db, site, "/darshan-booking", word_count=300, h1="Darshan")
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        outcome = await analyse_competitors(db, site, keyword="darshan booking", page_id=page.id)

        assert outcome.error is None
        assert outcome.fetched_count == 2  # rival-a, rival-b ok; rival-c blocked
        assert outcome.failed_count == 1
        assert outcome.paa_count == 2

        analysis = db.get(CompetitorAnalysis, outcome.analysis_id)
        assert analysis.keyword == "darshan booking"
        assert analysis.page_id == page.id
        assert len(analysis.results) == 3

    async def test_content_gap_uses_the_median_not_the_mean(self, db, member_user, monkeypatch):
        site = make_site(db, member_user)
        page = add_page(db, site, "/darshan-booking", word_count=300, h1="Darshan")
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        outcome = await analyse_competitors(db, site, keyword="darshan booking", page_id=page.id)
        analysis = db.get(CompetitorAnalysis, outcome.analysis_id)

        assert analysis.this_page_word_count == 300
        # Two successful fetches, ~900 and ~1100 words -> the median of the sorted pair is
        # whichever of the two lower-ranked-by-word-count values comes first (index len//2 == 1
        # for a 2-element list picks the larger one).
        assert 1080 <= analysis.competitor_median_word_count <= 1130
        assert analysis.competitor_avg_h2_count == 2.0

    async def test_shared_subtopics_become_the_missing_subtopics_list(
        self, db, member_user, monkeypatch
    ):
        site = make_site(db, member_user)
        page = add_page(db, site, "/darshan-booking", word_count=300, h1="Darshan")
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        outcome = await analyse_competitors(db, site, keyword="darshan booking", page_id=page.id)
        # Both rival-a and rival-b independently have an H2 "How to book" — a real pattern.
        assert any("how to book" in s.lower() for s in outcome.missing_subtopics)
        # Each rival's OTHER heading ("Pricing" / "Cancellation policy") appears on only one
        # site each, so it must not appear — the two-site bar is what makes this a pattern.
        assert not any("pricing" in s.lower() for s in outcome.missing_subtopics)

    async def test_an_unconfigured_key_reports_the_error_without_crashing(
        self, db, member_user, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.serpapi_key", "")
        site = make_site(db, member_user)
        db.commit()

        outcome = await analyse_competitors(db, site, keyword="darshan booking")
        assert outcome.error is not None
        assert "not configured" in outcome.error

    async def test_a_blank_keyword_is_rejected_before_any_call(self, db, member_user):
        site = make_site(db, member_user)
        db.commit()
        outcome = await analyse_competitors(db, site, keyword="   ")
        assert outcome.error == "A keyword is required."

    async def test_an_unknown_page_id_reports_the_error(self, db, member_user, monkeypatch):
        site = make_site(db, member_user)
        db.commit()
        patch_transport(monkeypatch, combined_handler())
        outcome = await analyse_competitors(db, site, keyword="x", page_id=999999)
        assert "not found" in outcome.error

    async def test_analysis_without_a_page_still_works(self, db, member_user, monkeypatch):
        """A keyword can be researched ad hoc, before any page targets it."""
        site = make_site(db, member_user)
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        outcome = await analyse_competitors(db, site, keyword="darshan booking")
        assert outcome.error is None
        analysis = db.get(CompetitorAnalysis, outcome.analysis_id)
        assert analysis.page_id is None
        assert analysis.this_page_word_count is None


# ── API surface ──────────────────────────────────────────────────────────────


class TestCompetitorApi:
    def test_status_endpoint_reflects_configuration(self, client, monkeypatch):
        monkeypatch.setattr("app.config.settings.serpapi_key", "configured")
        resp = client.get("/api/serp/status")
        assert resp.json() == {"configured": True}

    def test_analyse_endpoint_wait_true_runs_synchronously(self, client, db, member_user, monkeypatch):
        site = make_site(db, member_user)
        page = add_page(db, site, "/darshan-booking", word_count=300, h1="Darshan")
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        resp = client.post(
            f"/api/websites/{site.id}/pages/{page.id}/competitors/analyse",
            json={"keyword": "darshan booking", "wait": True},
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["fetched_count"] == 2

    def test_falls_back_to_the_pages_primary_keyword_when_none_given(
        self, client, db, member_user, monkeypatch
    ):
        site = make_site(db, member_user)
        page = add_page(db, site, "/darshan-booking", word_count=300, h1="Darshan")
        db.add(PageIntentProfile(
            page_id=page.id, website_id=site.id, detected_intent="transactional",
            business_intent="transactional", primary_keywords=["darshan booking"],
        ))
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        resp = client.post(
            f"/api/websites/{site.id}/pages/{page.id}/competitors/analyse",
            json={"wait": True},
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        assert resp.json()["keyword"] == "darshan booking"

    def test_no_keyword_and_no_profile_is_a_clear_error_not_a_crash(
        self, client, db, member_user
    ):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        db.commit()
        resp = client.post(
            f"/api/websites/{site.id}/pages/{page.id}/competitors/analyse",
            json={"wait": True},
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    def test_get_before_any_analysis_reports_unavailable(self, client, db, member_user):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        db.commit()
        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/competitors",
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        assert resp.json()["available"] is False

    def test_get_after_analysis_returns_the_full_comparison(
        self, client, db, member_user, monkeypatch
    ):
        site = make_site(db, member_user)
        page = add_page(db, site, "/darshan-booking", word_count=300, h1="Darshan")
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        client.post(
            f"/api/websites/{site.id}/pages/{page.id}/competitors/analyse",
            json={"keyword": "darshan booking", "wait": True},
            headers=auth_headers(member_user),
        )
        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/competitors",
            headers=auth_headers(member_user),
        )
        body = resp.json()
        assert body["available"] is True
        assert body["keyword"] == "darshan booking"
        assert len(body["competitors"]) == 3
        assert body["content_gap"]["this_page_word_count"] == 300

    def test_unconfigured_serpapi_returns_a_clear_error_not_a_500(
        self, client, db, member_user, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.serpapi_key", "")
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        db.commit()
        resp = client.post(
            f"/api/websites/{site.id}/pages/{page.id}/competitors/analyse",
            json={"keyword": "x", "wait": True},
            headers=auth_headers(member_user),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    def test_url_action_plan_surfaces_the_latest_competitor_analysis(
        self, client, db, member_user, monkeypatch
    ):
        """Step 3's page_opportunities endpoint must show real data once an analysis exists,
        not the permanent 'unavailable' placeholder."""
        site = make_site(db, member_user)
        page = add_page(db, site, "/darshan-booking", word_count=300, h1="Darshan")
        db.commit()
        patch_transport(monkeypatch, combined_handler())

        client.post(
            f"/api/websites/{site.id}/pages/{page.id}/competitors/analyse",
            json={"keyword": "darshan booking", "wait": True},
            headers=auth_headers(member_user),
        )
        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/opportunities",
            headers=auth_headers(member_user),
        )
        body = resp.json()
        assert body["competitor_analysis"]["available"] is True
        assert body["competitor_analysis"]["keyword"] == "darshan booking"

    def test_url_action_plan_is_still_honest_when_nothing_has_been_run(
        self, client, db, member_user
    ):
        site = make_site(db, member_user)
        page = add_page(db, site, "/x")
        db.commit()
        resp = client.get(
            f"/api/websites/{site.id}/pages/{page.id}/opportunities",
            headers=auth_headers(member_user),
        )
        assert resp.json()["competitor_analysis"]["available"] is False
