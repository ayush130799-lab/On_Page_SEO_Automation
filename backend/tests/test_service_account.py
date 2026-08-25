"""Service-account (server-to-server) authentication for Search Console and GA4.

The authorization-code flow needs a human at a browser and, while the consent screen is
unverified, Google expires its refresh tokens after seven days. A service account has neither
problem, which makes it the right default for scheduled syncs — so this path is tested as
thoroughly as the interactive one.

The RSA key is generated rather than hard-coded so the RS256 signing path is genuinely
exercised; a fake PEM would fail at signing and the tests would prove nothing.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from app.models import (
    GSCMetric,
    Integration,
    IntegrationProvider,
    MemberRole,
    Page,
    Website,
    WebsiteMember,
)
from app.services.integrations import ga4, google_oauth, gsc
from app.services.integrations.base import read_credentials, upsert_integration
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
    for path in ["/", "/products"]:
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


def patch_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def make_key() -> dict:
    """A structurally real service-account key with a genuine RSA private key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    return {
        "type": "service_account",
        "project_id": "seo-automation-test",
        "private_key_id": "abc123",
        "private_key": pem,
        "client_email": "seo-bot@seo-automation-test.iam.gserviceaccount.com",
        "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


# ── Key parsing ─────────────────────────────────────────────────────────────


class TestKeyParsing:
    def test_a_valid_key_is_reduced_to_what_is_stored(self):
        parsed = google_oauth.parse_service_account_key(make_key())
        assert parsed["type"] == "service_account"
        assert parsed["client_email"].endswith(".iam.gserviceaccount.com")
        assert "-----BEGIN" in parsed["private_key"]
        # client_id is not needed, so it is not carried into storage.
        assert "client_id" not in parsed

    def test_a_json_string_is_accepted(self):
        raw = json.dumps(make_key())
        assert google_oauth.parse_service_account_key(raw)["type"] == "service_account"

    def test_malformed_json_is_rejected_with_guidance(self):
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError, match="not valid JSON"):
            google_oauth.parse_service_account_key("{not json")

    def test_an_oauth_client_file_is_rejected(self):
        """People routinely download the wrong file from the same console page."""
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError, match="not a service account"):
            google_oauth.parse_service_account_key(
                {"type": "authorized_user", "client_id": "x", "client_secret": "y"}
            )

    def test_a_key_missing_required_fields_is_rejected(self):
        from app.core.errors import ValidationError

        key = make_key()
        del key["client_email"]
        with pytest.raises(ValidationError, match="client_email"):
            google_oauth.parse_service_account_key(key)

    def test_a_mangled_private_key_is_rejected(self):
        """Pasting JSON through a shell frequently destroys the PEM newlines."""
        from app.core.errors import ValidationError

        key = make_key()
        key["private_key"] = "not-a-pem-key"
        with pytest.raises(ValidationError, match="PEM"):
            google_oauth.parse_service_account_key(key)


# ── Assertion signing and token minting ─────────────────────────────────────


class TestTokenMinting:
    def test_the_assertion_carries_the_claims_google_requires(self):
        import jwt

        credentials = google_oauth.parse_service_account_key(make_key())
        assertion = google_oauth._build_assertion(credentials, google_oauth.SCOPES["gsc"])

        # Decoded without verification — the claim shape is what is under test.
        claims = jwt.decode(assertion, options={"verify_signature": False})
        assert claims["iss"] == credentials["client_email"]
        assert claims["aud"] == "https://oauth2.googleapis.com/token"
        assert "webmasters.readonly" in claims["scope"]
        assert claims["exp"] - claims["iat"] == 3600  # Google caps this at one hour
        assert jwt.get_unverified_header(assertion)["alg"] == "RS256"

    def test_the_assertion_is_verifiable_with_the_matching_public_key(self):
        """Proves the signature is real, not merely well-formed."""
        import jwt
        from cryptography.hazmat.primitives import serialization

        key = make_key()
        credentials = google_oauth.parse_service_account_key(key)
        assertion = google_oauth._build_assertion(credentials, google_oauth.SCOPES["ga4"])

        private_key = serialization.load_pem_private_key(
            key["private_key"].encode(), password=None
        )
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        decoded = jwt.decode(
            assertion,
            public_pem,
            algorithms=["RS256"],
            audience="https://oauth2.googleapis.com/token",
        )
        assert decoded["iss"] == credentials["client_email"]

    def test_scopes_differ_per_provider(self):
        import jwt

        credentials = google_oauth.parse_service_account_key(make_key())
        gsc_claims = jwt.decode(
            google_oauth._build_assertion(credentials, google_oauth.SCOPES["gsc"]),
            options={"verify_signature": False},
        )
        ga4_claims = jwt.decode(
            google_oauth._build_assertion(credentials, google_oauth.SCOPES["ga4"]),
            options={"verify_signature": False},
        )
        assert "webmasters.readonly" in gsc_claims["scope"]
        assert "analytics.readonly" in ga4_claims["scope"]

    async def test_a_token_is_minted_with_the_jwt_bearer_grant(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["body"] = request.read().decode()
            return httpx.Response(200, json={"access_token": "sa-token", "expires_in": 3600})

        patch_transport(monkeypatch, handler)
        credentials = google_oauth.parse_service_account_key(make_key())
        payload = await google_oauth.mint_service_account_token(credentials, "gsc")

        assert payload["access_token"] == "sa-token"
        assert (
            "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer"
            in captured["body"]
        )
        assert "assertion=" in captured["body"]

    async def test_a_rejected_key_is_reported_clearly(self, monkeypatch):
        patch_transport(
            monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid_grant"})
        )
        credentials = google_oauth.parse_service_account_key(make_key())

        from app.core.errors import IntegrationError

        with pytest.raises(IntegrationError, match="rejected the service account"):
            await google_oauth.verify_service_account(credentials, "gsc")


# ── Token lifecycle ─────────────────────────────────────────────────────────


class TestAccessTokenLifecycle:
    async def test_get_access_token_mints_instead_of_refreshing(self, db, site, monkeypatch):
        """A service account has no refresh token; the OAuth path would have raised."""
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={"access_token": "minted", "expires_in": 3600})

        patch_transport(monkeypatch, handler)
        credentials = google_oauth.parse_service_account_key(make_key())
        integration = upsert_integration(
            db, site, IntegrationProvider.GSC, credentials=credentials,
            config={"site_url": "sc-domain:acme.test"},
        )

        assert await google_oauth.get_access_token(db, integration) == "minted"
        assert calls["n"] == 1

        stored = read_credentials(integration)
        assert stored["type"] == "service_account"
        assert stored["access_token"] == "minted"
        assert integration.token_expires_at is not None

    async def test_a_cached_token_is_reused(self, db, site, monkeypatch):
        credentials = google_oauth.parse_service_account_key(make_key())
        credentials["access_token"] = "still-valid"
        credentials["expires_at"] = (google_oauth.utcnow() + timedelta(hours=1)).isoformat()
        integration = upsert_integration(
            db, site, IntegrationProvider.GSC, credentials=credentials
        )

        def explode(request):
            raise AssertionError("no token should be minted while the cached one is valid")

        patch_transport(monkeypatch, explode)
        assert await google_oauth.get_access_token(db, integration) == "still-valid"

    async def test_an_expired_token_is_re_minted(self, db, site, monkeypatch):
        credentials = google_oauth.parse_service_account_key(make_key())
        credentials["access_token"] = "stale"
        credentials["expires_at"] = (google_oauth.utcnow() - timedelta(hours=2)).isoformat()
        integration = upsert_integration(
            db, site, IntegrationProvider.GSC, credentials=credentials
        )

        patch_transport(
            monkeypatch,
            lambda r: httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}),
        )
        assert await google_oauth.get_access_token(db, integration) == "fresh"

    def test_the_two_auth_modes_are_distinguishable(self):
        assert google_oauth.is_service_account({"type": "service_account"}) is True
        assert google_oauth.is_service_account({"refresh_token": "rt"}) is False


# ── Connect endpoint ────────────────────────────────────────────────────────


def google_handler(*, sites=True, properties=True):
    def handler(request):
        url = str(request.url)
        if "oauth2.googleapis.com/token" in url:
            return httpx.Response(200, json={"access_token": "sa", "expires_in": 3600})
        if "/sites" in url:
            return httpx.Response(
                200,
                json=(
                    {
                        "siteEntry": [
                            {"siteUrl": "sc-domain:acme.test", "permissionLevel": "siteOwner"}
                        ]
                    }
                    if sites
                    else {}
                ),
            )
        if "accountSummaries" in url:
            return httpx.Response(
                200,
                json=(
                    {
                        "accountSummaries": [
                            {
                                "displayName": "Acme",
                                "propertySummaries": [
                                    {
                                        "property": "properties/412345678",
                                        "displayName": "Acme Web",
                                    }
                                ],
                            }
                        ]
                    }
                    if properties
                    else {}
                ),
            )
        return httpx.Response(404)

    return handler


class TestConnectEndpoint:
    def test_gsc_connects_and_auto_detects_the_property(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, google_handler())
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/service-account",
            json={"key": make_key()},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "connected"
        assert body["config"]["site_url"] == "sc-domain:acme.test"
        assert body["config"]["auth_mode"] == "service_account"
        assert body["account_label"].endswith(".iam.gserviceaccount.com")

    def test_ga4_connects_and_auto_selects_a_single_property(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, google_handler())
        response = client.post(
            f"/api/websites/{site.id}/integrations/ga4/service-account",
            json={"key": make_key()},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        assert response.json()["config"]["property_id"] == "412345678"

    def test_the_private_key_never_leaves_the_server(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, google_handler())
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/service-account",
            json={"key": make_key()}, headers=auth_headers(member_user),
        )
        assert "BEGIN PRIVATE KEY" not in response.text
        assert "private_key" not in response.text

        listing = client.get(
            f"/api/websites/{site.id}/integrations", headers=auth_headers(member_user)
        ).text
        assert "BEGIN PRIVATE KEY" not in listing

        stored = db.query(Integration).one().credentials_encrypted
        assert "BEGIN PRIVATE KEY" not in stored

    def test_a_service_account_without_property_access_is_refused(
        self, client, db, site, member_user, monkeypatch
    ):
        """The commonest mistake: a valid key that was never granted on the property."""
        patch_transport(monkeypatch, google_handler(sites=False))
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/service-account",
            json={"key": make_key()},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 422
        message = response.json()["error"]["message"]
        assert "Users and permissions" in message
        assert "iam.gserviceaccount.com" in message
        # Nothing is stored when the grant is missing.
        assert db.query(Integration).count() == 0

    def test_a_ga4_service_account_without_access_names_the_fix(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, google_handler(properties=False))
        response = client.post(
            f"/api/websites/{site.id}/integrations/ga4/service-account",
            json={"key": make_key()},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 422
        assert "Property access management" in response.json()["error"]["message"]

    def test_requesting_an_inaccessible_property_lists_what_is_available(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, google_handler())
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/service-account",
            json={"key": make_key(), "site_url": "sc-domain:wrong.test"},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 422
        assert "sc-domain:acme.test" in response.json()["error"]["message"]

    def test_an_explicit_property_is_honoured(
        self, client, db, site, member_user, monkeypatch
    ):
        patch_transport(monkeypatch, google_handler())
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/service-account",
            json={"key": make_key(), "site_url": "sc-domain:acme.test"},
            headers=auth_headers(member_user),
        )
        assert response.json()["config"]["site_url"] == "sc-domain:acme.test"

    def test_semrush_cannot_use_this_endpoint(self, client, site, member_user):
        response = client.post(
            f"/api/websites/{site.id}/integrations/semrush/service-account",
            json={"key": make_key()},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 422

    def test_a_stranger_cannot_connect_a_service_account(self, client, db, site):
        from .conftest import make_user

        stranger = make_user(db, email="sa-stranger@example.com")
        response = client.post(
            f"/api/websites/{site.id}/integrations/gsc/service-account",
            json={"key": make_key()},
            headers=auth_headers(stranger),
        )
        assert response.status_code == 404


# ── The point: syncs work unchanged ─────────────────────────────────────────


class TestSyncThroughServiceAccount:
    async def test_a_gsc_sync_works_with_service_account_auth(self, db, site, monkeypatch):
        """The sync path must not care which auth mode produced the token."""
        day = TODAY - timedelta(days=4)

        def handler(request):
            url = str(request.url)
            if "oauth2.googleapis.com/token" in url:
                return httpx.Response(200, json={"access_token": "sa", "expires_in": 3600})
            if "searchAnalytics/query" in url:
                if '"query"' in request.read().decode():
                    return httpx.Response(200, json={"rows": []})
                return httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "keys": [day.isoformat(), "https://acme.test/products"],
                                "clicks": 45, "impressions": 900, "ctr": 0.05, "position": 6.0,
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        patch_transport(monkeypatch, handler)
        credentials = google_oauth.parse_service_account_key(make_key())
        upsert_integration(
            db, site, IntegrationProvider.GSC, credentials=credentials,
            config={"site_url": "sc-domain:acme.test"},
        )

        summary = await gsc.sync(db, site, days=14, end=TODAY)
        assert summary["metrics_upserted"] == 1

        metric = db.query(GSCMetric).one()
        assert metric.clicks == 45
        assert metric.date == day

    async def test_a_ga4_sync_works_with_service_account_auth(self, db, site, monkeypatch):
        day = TODAY - timedelta(days=2)

        def handler(request):
            url = str(request.url)
            if "oauth2.googleapis.com/token" in url:
                return httpx.Response(200, json={"access_token": "sa", "expires_in": 3600})
            if ":runReport" in url:
                return httpx.Response(
                    200,
                    json={
                        "dimensionHeaders": [{"name": "date"}, {"name": "pagePath"}],
                        "metricHeaders": [
                            {"name": n}
                            for n in (
                                "totalUsers", "newUsers", "sessions", "screenPageViews",
                                "engagedSessions", "engagementRate", "userEngagementDuration",
                                "bounceRate", "conversions", "totalRevenue", "purchaseRevenue",
                            )
                        ],
                        "rows": [
                            {
                                "dimensionValues": [
                                    {"value": day.strftime("%Y%m%d")}, {"value": "/products"}
                                ],
                                "metricValues": [
                                    {"value": "500"}, {"value": "300"}, {"value": "600"},
                                    {"value": "700"}, {"value": "400"}, {"value": "0.7"},
                                    {"value": "18000"}, {"value": "0.3"}, {"value": "9"},
                                    {"value": "1200"}, {"value": "1100"},
                                ],
                            }
                        ],
                        "rowCount": 1,
                        "metadata": {"currencyCode": "USD"},
                    },
                )
            return httpx.Response(404)

        patch_transport(monkeypatch, handler)
        credentials = google_oauth.parse_service_account_key(make_key())
        upsert_integration(
            db, site, IntegrationProvider.GA4, credentials=credentials,
            config={"property_id": "412345678"},
        )

        summary = await ga4.sync(db, site, days=14, end=TODAY)
        assert summary["metrics_upserted"] == 1

        from app.models import GA4Metric

        metric = db.query(GA4Metric).one()
        assert metric.users == 500
        assert metric.revenue == 1100.0  # purchaseRevenue preferred
