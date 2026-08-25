"""Integration connectors: OAuth, page matching, and the GSC / GA4 / Semrush syncs.

Provider responses are mocked with payloads shaped exactly like the real APIs (GA4's positional
header/row format, Search Console's ``keys`` arrays, Semrush's semicolon CSV) so the parsing code
is genuinely exercised rather than fed a convenient shape.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from app.core.crypto import decrypt_json
from app.models import (
    GA4Metric,
    GSCMetric,
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    MemberRole,
    Page,
    SemrushMetric,
    Website,
    WebsiteMember,
)
from app.services.integrations import ga4, google_oauth, gsc, semrush
from app.services.integrations.base import read_credentials, upsert_integration
from app.services.integrations.matching import PageResolver, site_url_variants
from app.services.metrics import aggregate_page_metrics
from app.utils.url_utils import url_hash, url_path

from .conftest import auth_headers

TODAY = date(2026, 6, 15)


@pytest.fixture
def site(db, member_user):
    website = Website(
        name="Acme", url="https://acme.test/", domain="acme.test",
        created_by_id=member_user.id,
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))

    for path in ["/", "/products", "/blog/post-one", "/about"]:
        url = f"https://acme.test{path}" if path != "/" else "https://acme.test/"
        db.add(
            Page(
                website_id=website.id, url=url, url_hash=url_hash(url),
                path=url_path(url), seo_score=80.0,
            )
        )
    db.commit()
    db.refresh(website)
    return website


def page_id_for(db, website, path):
    return db.query(Page).filter(Page.website_id == website.id, Page.path == path).one().id


def patch_transport(monkeypatch, handler):
    """Route every integration HTTP client through a mock transport."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


# ── Credential storage ──────────────────────────────────────────────────────


class TestCredentialStorage:
    def test_credentials_are_encrypted_at_rest(self, db, site):
        integration = upsert_integration(
            db, site, IntegrationProvider.SEMRUSH,
            credentials={"api_key": "super-secret-semrush-key"},
            config={"database": "us"},
        )
        assert "super-secret-semrush-key" not in integration.credentials_encrypted
        assert read_credentials(integration)["api_key"] == "super-secret-semrush-key"
        assert decrypt_json(integration.credentials_encrypted)["api_key"] == (
            "super-secret-semrush-key"
        )

    def test_config_merges_rather_than_replaces(self, db, site):
        upsert_integration(db, site, IntegrationProvider.GSC, config={"site_url": "a"})
        integration = upsert_integration(db, site, IntegrationProvider.GSC, config={"extra": 1})
        assert integration.config == {"site_url": "a", "extra": 1}

    def test_disconnect_clears_credentials_but_keeps_metrics(self, db, site):
        from app.services.integrations.base import disconnect

        integration = upsert_integration(
            db, site, IntegrationProvider.SEMRUSH, credentials={"api_key": "k"}
        )
        db.add(
            SemrushMetric(
                website_id=site.id, page_id=page_id_for(db, site, "/"), date=TODAY,
                organic_keywords=12,
            )
        )
        db.commit()

        disconnect(db, integration)
        assert integration.credentials_encrypted is None
        assert integration.status == IntegrationStatus.NOT_CONNECTED
        assert db.query(SemrushMetric).count() == 1

    def test_the_api_never_returns_credentials(self, client, db, site, member_user):
        upsert_integration(
            db, site, IntegrationProvider.SEMRUSH, credentials={"api_key": "leak-me"}
        )
        body = client.get(
            f"/api/websites/{site.id}/integrations", headers=auth_headers(member_user)
        ).text
        assert "leak-me" not in body
        assert "credentials_encrypted" not in body


# ── Page matching ───────────────────────────────────────────────────────────


class TestPageResolver:
    def test_matches_absolute_urls_paths_and_variants(self, db, site):
        resolver = PageResolver.build(db, site.id, site.url)
        expected = page_id_for(db, site, "/products")

        for candidate in (
            "https://acme.test/products",
            "https://acme.test/products/",
            "https://www.acme.test/products",
            "http://acme.test/products",
            "/products",
            "products",
        ):
            assert resolver.resolve(candidate) == expected, candidate

    def test_unknown_urls_are_counted_not_guessed(self, db, site):
        resolver = PageResolver.build(db, site.id, site.url)
        assert resolver.resolve("https://acme.test/does-not-exist") is None
        assert resolver.resolve("") is None
        assert resolver.unmatched == 2
        assert resolver.summary["unmatched_samples"]

    def test_search_console_property_variants(self):
        variants = site_url_variants("https://www.acme.test/")
        assert "sc-domain:acme.test" in variants
        assert "https://www.acme.test/" in variants


# ── Google OAuth ────────────────────────────────────────────────────────────


class TestGoogleOAuth:
    def test_authorization_url_requests_offline_access(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "client-id")
        monkeypatch.setattr("app.config.settings.google_client_secret", "client-secret")

        url = google_oauth.build_authorization_url(7, "gsc", 3)
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "webmasters.readonly" in url

    def test_state_round_trips_and_rejects_tampering(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "client-id")
        monkeypatch.setattr("app.config.settings.google_client_secret", "client-secret")

        url = google_oauth.build_authorization_url(7, "ga4", 3)
        state = url.split("state=")[1].split("&")[0]
        claims = google_oauth.parse_state(state)
        assert claims["website_id"] == 7 and claims["provider"] == "ga4"

        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            google_oauth.parse_state(state[:-4] + "abcd")

    def test_unconfigured_oauth_is_reported_clearly(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "")
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError, match="not configured"):
            google_oauth.build_authorization_url(1, "gsc", 1)

    async def test_a_missing_refresh_token_is_rejected(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "id")
        monkeypatch.setattr("app.config.settings.google_client_secret", "secret")
        patch_transport(
            monkeypatch,
            lambda r: httpx.Response(200, json={"access_token": "at", "expires_in": 3600}),
        )
        from app.core.errors import IntegrationError

        with pytest.raises(IntegrationError, match="refresh token"):
            await google_oauth.exchange_code("code")

    async def test_expired_access_token_is_refreshed_and_persisted(self, db, site, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "id")
        monkeypatch.setattr("app.config.settings.google_client_secret", "secret")

        stale = (google_oauth.utcnow() - timedelta(hours=2)).isoformat()
        integration = upsert_integration(
            db, site, IntegrationProvider.GSC,
            credentials={"refresh_token": "rt", "access_token": "old", "expires_at": stale},
        )

        patch_transport(
            monkeypatch,
            lambda r: httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}),
        )
        token = await google_oauth.get_access_token(db, integration)

        assert token == "fresh"
        stored = read_credentials(integration)
        assert stored["access_token"] == "fresh"
        assert stored["refresh_token"] == "rt"  # preserved across refresh

    async def test_a_valid_token_is_reused_without_a_network_call(self, db, site, monkeypatch):
        future = (google_oauth.utcnow() + timedelta(hours=1)).isoformat()
        integration = upsert_integration(
            db, site, IntegrationProvider.GSC,
            credentials={"refresh_token": "rt", "access_token": "still-good", "expires_at": future},
        )

        def explode(request):
            raise AssertionError("no HTTP call should be made for a valid token")

        patch_transport(monkeypatch, explode)
        assert await google_oauth.get_access_token(db, integration) == "still-good"


# ── Search Console ──────────────────────────────────────────────────────────


def gsc_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "oauth2.googleapis.com/token" in url:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    if "/sites" in url and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "siteEntry": [
                    {"siteUrl": "sc-domain:acme.test", "permissionLevel": "siteOwner"},
                    {"siteUrl": "https://other.test/", "permissionLevel": "siteFullUser"},
                ]
            },
        )
    if "searchAnalytics/query" in url:
        body = request.read().decode()
        if '"query"' in body:
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {"keys": ["https://acme.test/products", "buy widgets"],
                         "clicks": 40, "impressions": 900, "ctr": 0.044, "position": 6.2},
                        {"keys": ["https://acme.test/products", "widget prices"],
                         "clicks": 12, "impressions": 400, "ctr": 0.03, "position": 11.4},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "rows": [
                    {"keys": ["2026-06-10", "https://acme.test/products"],
                     "clicks": 30, "impressions": 700, "ctr": 0.043, "position": 6.5},
                    {"keys": ["2026-06-11", "https://acme.test/products"],
                     "clicks": 22, "impressions": 600, "ctr": 0.037, "position": 7.1},
                    {"keys": ["2026-06-10", "https://acme.test/"],
                     "clicks": 100, "impressions": 2000, "ctr": 0.05, "position": 2.1},
                    # A URL the crawler never saw — must be skipped, not invented.
                    {"keys": ["2026-06-10", "https://acme.test/ghost"],
                     "clicks": 5, "impressions": 10, "ctr": 0.5, "position": 1.0},
                ]
            },
        )
    return httpx.Response(404)


class TestSearchConsoleSync:
    @pytest.fixture
    def connected(self, db, site, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "id")
        monkeypatch.setattr("app.config.settings.google_client_secret", "secret")
        upsert_integration(
            db, site, IntegrationProvider.GSC,
            credentials={
                "refresh_token": "rt", "access_token": "at",
                "expires_at": (google_oauth.utcnow() + timedelta(hours=1)).isoformat(),
            },
            config={"site_url": "sc-domain:acme.test"},
        )
        patch_transport(monkeypatch, gsc_handler)
        return site

    async def test_property_auto_detection(self, db, connected, monkeypatch):
        detected = await gsc.detect_site_url("at", connected)
        assert detected == "sc-domain:acme.test"

    async def test_sync_writes_daily_page_metrics(self, db, connected):
        summary = await gsc.sync(db, connected, days=7, end=TODAY)

        assert summary["metrics_upserted"] == 3
        assert summary["unmatched"] == 1  # the /ghost URL

        products = page_id_for(db, connected, "/products")
        rows = db.query(GSCMetric).filter(GSCMetric.page_id == products).all()
        assert {r.date for r in rows} == {date(2026, 6, 10), date(2026, 6, 11)}
        first = next(r for r in rows if r.date == date(2026, 6, 10))
        assert first.clicks == 30 and first.impressions == 700
        assert first.ctr == pytest.approx(30 / 700, rel=1e-3)

    async def test_top_queries_are_attached_to_the_pages_newest_data_point(self, db, connected):
        """Queries describe the whole window, so they hang off the latest day that has data."""
        await gsc.sync(db, connected, days=7, end=TODAY)
        rows = (
            db.query(GSCMetric)
            .filter(GSCMetric.page_id == page_id_for(db, connected, "/products"))
            .all()
        )
        with_queries = [r for r in rows if r.queries]
        assert len(with_queries) == 1
        assert with_queries[0].date == date(2026, 6, 11)
        assert [q["query"] for q in with_queries[0].queries] == ["buy widgets", "widget prices"]

    async def test_queries_never_create_an_empty_day(self, db, connected):
        """A zero-click row invented just to hold queries would skew every average."""
        await gsc.sync(db, connected, days=7, end=TODAY)
        assert db.query(GSCMetric).filter(GSCMetric.impressions == 0).count() == 0

    async def test_resyncing_the_same_window_is_idempotent(self, db, connected):
        await gsc.sync(db, connected, days=7, end=TODAY)
        count_after_first = db.query(GSCMetric).count()
        await gsc.sync(db, connected, days=7, end=TODAY)
        assert db.query(GSCMetric).count() == count_after_first

    async def test_sync_marks_the_integration_successful(self, db, connected):
        await gsc.sync(db, connected, days=7, end=TODAY)
        integration = (
            db.query(Integration)
            .filter(Integration.provider == IntegrationProvider.GSC)
            .one()
        )
        assert integration.status == IntegrationStatus.CONNECTED
        assert integration.last_sync_status == "success"
        assert integration.sync_count == 1

    async def test_a_provider_failure_is_recorded_without_the_response_body(
        self, db, site, monkeypatch
    ):
        upsert_integration(
            db, site, IntegrationProvider.GSC,
            credentials={
                "refresh_token": "rt", "access_token": "at",
                "expires_at": (google_oauth.utcnow() + timedelta(hours=1)).isoformat(),
            },
            config={"site_url": "sc-domain:acme.test"},
        )
        patch_transport(
            monkeypatch,
            lambda r: httpx.Response(403, text="key=leaked-secret-value not authorised"),
        )

        with pytest.raises(Exception):
            await gsc.sync(db, site, days=3, end=TODAY)

        integration = (
            db.query(Integration).filter(Integration.provider == IntegrationProvider.GSC).one()
        )
        assert integration.status == IntegrationStatus.ERROR
        assert "leaked-secret-value" not in (integration.last_error or "")

    async def test_sync_without_a_selected_property_is_refused(self, db, site, monkeypatch):
        upsert_integration(db, site, IntegrationProvider.GSC, credentials={"refresh_token": "rt"})
        from app.core.errors import IntegrationError

        with pytest.raises(IntegrationError, match="property"):
            await gsc.sync(db, site, days=3, end=TODAY)


# ── GA4 ─────────────────────────────────────────────────────────────────────


GA4_REPORT = {
    "dimensionHeaders": [{"name": "date"}, {"name": "pagePath"}],
    "metricHeaders": [
        {"name": "totalUsers"}, {"name": "newUsers"}, {"name": "sessions"},
        {"name": "screenPageViews"}, {"name": "engagedSessions"}, {"name": "engagementRate"},
        {"name": "userEngagementDuration"}, {"name": "bounceRate"}, {"name": "conversions"},
        {"name": "totalRevenue"}, {"name": "purchaseRevenue"},
    ],
    "rows": [
        {
            "dimensionValues": [{"value": "20260610"}, {"value": "/products"}],
            "metricValues": [
                {"value": "1200"}, {"value": "800"}, {"value": "1500"}, {"value": "1800"},
                {"value": "1000"}, {"value": "0.67"}, {"value": "45000"}, {"value": "0.33"},
                {"value": "35"}, {"value": "5200.5"}, {"value": "5000.25"},
            ],
        },
        {
            "dimensionValues": [{"value": "20260610"}, {"value": "/"}],
            "metricValues": [
                {"value": "5000"}, {"value": "3000"}, {"value": "6000"}, {"value": "7000"},
                {"value": "4000"}, {"value": "0.66"}, {"value": "120000"}, {"value": "0.34"},
                {"value": "5"}, {"value": "100"}, {"value": "0"},
            ],
        },
        {
            "dimensionValues": [{"value": "20260610"}, {"value": "/unknown-path"}],
            "metricValues": [{"value": "9"}] * 11,
        },
    ],
    "rowCount": 3,
    "metadata": {"currencyCode": "USD"},
}


def ga4_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "oauth2.googleapis.com/token" in url:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    if "accountSummaries" in url:
        return httpx.Response(
            200,
            json={
                "accountSummaries": [
                    {
                        "displayName": "Acme Inc",
                        "propertySummaries": [
                            {"property": "properties/412345678", "displayName": "Acme Web"}
                        ],
                    }
                ]
            },
        )
    if ":runReport" in url:
        return httpx.Response(200, json=GA4_REPORT)
    return httpx.Response(404)


class TestGA4Sync:
    @pytest.fixture
    def connected(self, db, site, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "id")
        monkeypatch.setattr("app.config.settings.google_client_secret", "secret")
        upsert_integration(
            db, site, IntegrationProvider.GA4,
            credentials={
                "refresh_token": "rt", "access_token": "at",
                "expires_at": (google_oauth.utcnow() + timedelta(hours=1)).isoformat(),
            },
            config={"property_id": "412345678"},
        )
        patch_transport(monkeypatch, ga4_handler)
        return site

    async def test_property_discovery(self, db, connected):
        properties = await ga4.list_properties("at")
        assert properties == [
            {"property_id": "412345678", "display_name": "Acme Web", "account": "Acme Inc"}
        ]

    async def test_positional_report_format_is_parsed(self, db, connected):
        await ga4.sync(db, connected, days=7, end=TODAY)

        row = (
            db.query(GA4Metric)
            .filter(GA4Metric.page_id == page_id_for(db, connected, "/products"))
            .one()
        )
        assert row.date == date(2026, 6, 10)
        assert row.users == 1200
        assert row.sessions == 1500
        assert row.conversions == 35
        assert row.engagement_rate == pytest.approx(0.67, rel=1e-3)
        assert row.average_engagement_time == pytest.approx(30.0, rel=1e-2)
        assert row.currency == "USD"

    async def test_purchase_revenue_is_preferred_but_falls_back(self, db, connected):
        await ga4.sync(db, connected, days=7, end=TODAY)

        products = db.query(GA4Metric).filter(
            GA4Metric.page_id == page_id_for(db, connected, "/products")
        ).one()
        home = db.query(GA4Metric).filter(
            GA4Metric.page_id == page_id_for(db, connected, "/")
        ).one()

        assert products.revenue == 5000.25  # purchaseRevenue wins where present
        assert home.revenue == 100.0        # falls back to totalRevenue

    async def test_unmatched_paths_are_skipped(self, db, connected):
        summary = await ga4.sync(db, connected, days=7, end=TODAY)
        assert summary["metrics_upserted"] == 2
        assert summary["unmatched"] == 1

    async def test_resync_is_idempotent(self, db, connected):
        await ga4.sync(db, connected, days=7, end=TODAY)
        first = db.query(GA4Metric).count()
        await ga4.sync(db, connected, days=7, end=TODAY)
        assert db.query(GA4Metric).count() == first


# ── Semrush ─────────────────────────────────────────────────────────────────


DOMAIN_ORGANIC_CSV = (
    "Url;Number of Keywords;Traffic;Traffic (%)\r\n"
    "https://acme.test/products;42;3100;55\r\n"
    "https://acme.test/blog/post-one;18;700;12\r\n"
    "https://acme.test/not-crawled;3;10;1\r\n"
)
URL_ORGANIC_CSV = (
    "Keyword;Position;Search Volume;CPC;Competition;Keyword Difficulty Index;Traffic (%);Traffic Cost\r\n"
    "buy widgets;3;12000;2.40;0.8;61;40;900\r\n"
    "widget prices;7;8000;1.90;0.6;54;25;500\r\n"
    "industrial widgets;14;5000;3.10;0.7;66;15;300\r\n"
    "widget suppliers;28;2000;1.10;0.4;48;5;100\r\n"
)
BACKLINKS_CSV = "target;backlinks_num;domains_num\r\nacme.test/products;540;72\r\n"


def semrush_handler(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    if "countapiunits" in str(request.url):
        return httpx.Response(200, text="1250000")
    report = params.get("type")
    if report == "domain_organic_unique":
        return httpx.Response(200, text=DOMAIN_ORGANIC_CSV)
    if report == "url_organic":
        return httpx.Response(200, text=URL_ORGANIC_CSV)
    if report == "backlinks_overview":
        return httpx.Response(200, text=BACKLINKS_CSV)
    return httpx.Response(200, text="ERROR 50 :: NOTHING FOUND")


class TestSemrushSync:
    @pytest.fixture
    def connected(self, db, site, monkeypatch):
        upsert_integration(
            db, site, IntegrationProvider.SEMRUSH,
            credentials={"api_key": "semrush-key"},
            config={"database": "us", "max_pages": 50},
        )
        patch_transport(monkeypatch, semrush_handler)
        return site

    async def test_api_key_verification_returns_the_unit_balance(self, monkeypatch):
        patch_transport(monkeypatch, semrush_handler)
        result = await semrush.verify_api_key("semrush-key")
        assert result["api_units_remaining"] == 1250000
        assert "semrush-key" not in str(result["key_hint"])

    async def test_semicolon_csv_is_parsed(self, db, connected):
        keywords = await semrush.fetch_url_keywords(
            "k", "https://acme.test/products", "us"
        )
        assert len(keywords) == 4
        assert keywords[0] == {
            "keyword": "buy widgets", "position": 3, "volume": 12000, "cpc": 2.40,
            "competition": 0.8, "difficulty": 61.0, "traffic_percent": 40.0,
        }

    async def test_nothing_found_is_an_empty_result_not_an_error(self, monkeypatch):
        patch_transport(
            monkeypatch, lambda r: httpx.Response(200, text="ERROR 50 :: NOTHING FOUND")
        )
        assert await semrush.fetch_url_keywords("k", "https://acme.test/x", "us") == []

    async def test_a_real_semrush_error_is_raised(self, monkeypatch):
        patch_transport(
            monkeypatch, lambda r: httpx.Response(200, text="ERROR 120 :: WRONG KEY")
        )
        with pytest.raises(semrush.SemrushError, match="WRONG KEY"):
            await semrush.fetch_url_keywords("k", "https://acme.test/x", "us")

    def test_striking_distance_is_positions_4_to_20(self):
        summary = semrush.summarise_keywords(
            [
                {"keyword": "a", "position": 3, "volume": 12000},   # already top 3
                {"keyword": "b", "position": 7, "volume": 8000},    # striking distance
                {"keyword": "c", "position": 14, "volume": 5000},   # striking distance
                {"keyword": "d", "position": 28, "volume": 2000},   # too far back
            ]
        )
        assert summary["organic_keywords"] == 4
        assert summary["striking_distance_keywords"] == 2
        assert summary["opportunity_volume"] == 13000
        assert summary["best_position"] == 3
        assert summary["average_position"] == 13.0

    async def test_sync_stores_opportunity_metrics(self, db, connected):
        summary = await semrush.sync(db, connected, today=TODAY)
        assert summary["metrics_upserted"] == 2  # the third URL was never crawled

        row = (
            db.query(SemrushMetric)
            .filter(SemrushMetric.page_id == page_id_for(db, connected, "/products"))
            .one()
        )
        assert row.organic_keywords == 4
        assert row.striking_distance_keywords == 2
        assert row.opportunity_volume == 13000
        assert row.organic_traffic == 3100
        assert row.backlinks == 540
        assert row.referring_domains == 72
        assert len(row.keywords) == 4

    async def test_keyword_opportunities_are_ranked_by_volume(self, db, connected):
        await semrush.sync(db, connected, today=TODAY)
        opportunities = semrush.keyword_opportunities(db, connected.id)

        assert opportunities
        assert all(4 <= o["position"] <= 20 for o in opportunities)
        volumes = [o["volume"] for o in opportunities]
        assert volumes == sorted(volumes, reverse=True)

    async def test_a_domain_with_no_visibility_is_not_an_error(self, db, site, monkeypatch):
        upsert_integration(
            db, site, IntegrationProvider.SEMRUSH, credentials={"api_key": "k"}
        )
        patch_transport(
            monkeypatch, lambda r: httpx.Response(200, text="ERROR 50 :: NOTHING FOUND")
        )
        summary = await semrush.sync(db, site, today=TODAY)
        assert summary["metrics_upserted"] == 0

        integration = db.query(Integration).filter(
            Integration.provider == IntegrationProvider.SEMRUSH
        ).one()
        assert integration.status == IntegrationStatus.CONNECTED


# ── Aggregation across providers ────────────────────────────────────────────


class TestMetricAggregation:
    def test_metrics_are_summed_over_the_window(self, db, site):
        page_id = page_id_for(db, site, "/products")
        for offset in range(5):
            day = TODAY - timedelta(days=offset)
            db.add(
                GSCMetric(website_id=site.id, page_id=page_id, date=day,
                          clicks=10, impressions=200, ctr=0.05, position=5.0)
            )
            db.add(
                GA4Metric(website_id=site.id, page_id=page_id, date=day,
                          users=100, sessions=120, conversions=2, revenue=250.0)
            )
        db.commit()

        aggregated = aggregate_page_metrics(db, [page_id], window_days=30, today=TODAY)[page_id]
        assert aggregated["clicks"] == 50
        assert aggregated["impressions"] == 1000
        assert aggregated["ctr"] == pytest.approx(0.05)
        assert aggregated["users"] == 500
        assert aggregated["conversions"] == 10
        assert aggregated["revenue"] == 1250.0

    def test_rows_outside_the_window_are_excluded(self, db, site):
        page_id = page_id_for(db, site, "/products")
        db.add(
            GSCMetric(website_id=site.id, page_id=page_id, date=TODAY - timedelta(days=100),
                      clicks=999, impressions=999)
        )
        db.commit()
        aggregated = aggregate_page_metrics(db, [page_id], window_days=28, today=TODAY)[page_id]
        assert aggregated["clicks"] == 0

    def test_pages_without_metrics_return_zeroes(self, db, site):
        page_id = page_id_for(db, site, "/about")
        aggregated = aggregate_page_metrics(db, [page_id], today=TODAY)[page_id]
        assert aggregated["users"] == 0 and aggregated["clicks"] == 0
        assert aggregated["ctr"] is None

    def test_only_the_latest_semrush_snapshot_is_used(self, db, site):
        page_id = page_id_for(db, site, "/products")
        db.add(
            SemrushMetric(website_id=site.id, page_id=page_id, date=TODAY - timedelta(days=7),
                          organic_keywords=10, striking_distance_keywords=1)
        )
        db.add(
            SemrushMetric(website_id=site.id, page_id=page_id, date=TODAY,
                          organic_keywords=42, striking_distance_keywords=9)
        )
        db.commit()

        aggregated = aggregate_page_metrics(db, [page_id], window_days=30, today=TODAY)[page_id]
        assert aggregated["organic_keywords"] == 42
        assert aggregated["striking_distance_keywords"] == 9


# ── API surface ─────────────────────────────────────────────────────────────


class TestIntegrationApi:
    def test_semrush_connect_verifies_the_key_first(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, semrush_handler)
        response = client.post(
            f"/api/websites/{site.id}/integrations/semrush",
            json={"api_key": "semrush-key", "database": "uk", "max_pages": 100},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "connected"
        assert body["config"]["database"] == "uk"
        assert "semrush-key" not in response.text

    def test_a_rejected_semrush_key_is_not_stored(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, lambda r: httpx.Response(200, text="ERROR :: bad key"))
        response = client.post(
            f"/api/websites/{site.id}/integrations/semrush",
            json={"api_key": "wrong-key"},
            headers=auth_headers(member_user),
        )
        assert response.status_code >= 400
        assert db.query(Integration).count() == 0

    def test_github_connect_maps_the_repository(self, client, db, site, member_user):
        response = client.post(
            f"/api/websites/{site.id}/integrations/github",
            json={
                "repo": "https://github.com/acme/website.git",
                "branch": "production",
                "webhook_secret": "a-long-webhook-secret",
                "framework": "next",
            },
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        assert "a-long-webhook-secret" not in response.text

        db.refresh(site)
        assert site.github_repo == "acme/website"
        assert site.github_branch == "production"
        assert site.github_framework == "next"

    def test_authorize_requires_google_configuration(self, client, site, member_user, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "")
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/authorize",
            headers=auth_headers(member_user),
        )
        assert response.status_code == 422

    def test_authorize_returns_a_consent_url(self, client, site, member_user, monkeypatch):
        monkeypatch.setattr("app.config.settings.google_client_id", "id")
        monkeypatch.setattr("app.config.settings.google_client_secret", "secret")
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/authorize",
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        assert response.json()["authorization_url"].startswith("https://accounts.google.com/")

    def test_sync_of_an_unconnected_provider_is_refused(self, client, site, member_user):
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/sync",
            json={}, headers=auth_headers(member_user),
        )
        assert response.status_code == 404

    def test_sync_all_reports_per_provider_status(
        self, client, db, site, member_user, monkeypatch
    ):
        monkeypatch.setattr("app.api.routes.integrations.dispatch_sync", lambda *a, **k: "test")
        upsert_integration(
            db, site, IntegrationProvider.SEMRUSH, credentials={"api_key": "k"}
        )
        response = client.post(
            f"/api/websites/{site.id}/integrations/sync-all",
            json={}, headers=auth_headers(member_user),
        )
        statuses = {r["provider"]: r["status"] for r in response.json()}
        assert statuses["semrush"] == "queued"
        assert statuses["gsc"] == "skipped"

    def test_disconnect_clears_the_integration(self, client, db, site, member_user):
        upsert_integration(
            db, site, IntegrationProvider.SEMRUSH, credentials={"api_key": "k"}
        )
        response = client.delete(
            f"/api/websites/{site.id}/integrations/semrush", headers=auth_headers(member_user)
        )
        assert response.status_code == 200
        db.expire_all()
        integration = db.query(Integration).filter(
            Integration.provider == IntegrationProvider.SEMRUSH
        ).one()
        assert integration.credentials_encrypted is None

    def test_integrations_are_not_visible_to_other_users(self, client, db, site):
        from .conftest import make_user

        stranger = make_user(db, email="nosy@example.com")
        assert client.get(
            f"/api/websites/{site.id}/integrations", headers=auth_headers(stranger)
        ).status_code == 404
