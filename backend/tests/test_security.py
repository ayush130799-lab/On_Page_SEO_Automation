"""Security boundaries.

Everything here is a property that must hold regardless of how the rest of the system changes:
SSRF containment, the authorization boundary, credential confidentiality, and the fact that no
secret ever reaches a log line or an API response.
"""

from __future__ import annotations

import logging

import pytest

from app.core.crypto import encrypt_json
from app.core.logging import RedactingFilter, redact
from app.models import (
    Integration,
    IntegrationProvider,
    MemberRole,
    Page,
    UserRole,
    Website,
    WebsiteMember,
)
from app.services.integrations.base import upsert_integration
from app.utils.url_utils import is_safe_url
from app.utils.url_utils import url_hash, url_path

from .conftest import auth_headers, make_user


# ── SSRF ────────────────────────────────────────────────────────────────────


class TestSsrfProtection:
    """The crawler takes a user-supplied URL, so it must never become an internal proxy."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000",
            "http://127.0.0.1",
            "http://127.0.0.1:5432",
            "http://0.0.0.0",
            "http://[::1]:8000",
            "http://169.254.169.254/latest/meta-data",   # cloud instance metadata
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://10.0.0.5/admin",
            "http://192.168.1.1",
            "http://172.16.0.1",
        ],
    )
    def test_private_and_loopback_targets_are_refused(self, url):
        assert is_safe_url(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com",
            "file:///etc/passwd",
            "gopher://example.com",
            "data:text/html,<script>alert(1)</script>",
            "javascript:alert(1)",
            "",
            "not-a-url",
        ],
    )
    def test_non_http_schemes_are_refused(self, url):
        assert is_safe_url(url) is False

    def test_public_targets_are_permitted(self):
        assert is_safe_url("https://example.com") is True
        assert is_safe_url("https://www.google.com/search") is True

    def test_an_unresolvable_host_is_refused(self):
        """Failing closed matters: a host that cannot be checked must not be crawled."""
        assert is_safe_url("https://this-domain-definitely-does-not-exist-xyzq.invalid") is False

    def test_the_local_override_is_explicit(self):
        """`ALLOW_LOCAL_CRAWL` exists for tests and must never be the shipped default."""
        from app.config import Settings

        # The field default, not an instance: the test environment sets the variable.
        assert Settings.model_fields["allow_local_crawl"].default is False
        assert is_safe_url("http://127.0.0.1:8000", allow_local=True) is True

    async def test_the_crawler_refuses_an_unsafe_start_url(self):
        from app.services.crawler import CrawlConfig, Crawler

        crawler = Crawler("https://example.com/", CrawlConfig(allow_local=False))
        crawler.start_url = "http://169.254.169.254/latest/meta-data"
        with pytest.raises(ValueError, match="not a permitted crawl target"):
            await crawler.run()


# ── Authorization ───────────────────────────────────────────────────────────


@pytest.fixture
def other_site(db):
    owner = make_user(db, email="owner@other.example.com")
    website = Website(name="Other Co", url="https://other.example.com", domain="other.example.com")
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=owner.id, role=MemberRole.OWNER))

    url = "https://other.example.com/secret"
    page = Page(
        website_id=website.id, url=url, url_hash=url_hash(url), path=url_path(url),
        seo_score=50.0,
    )
    db.add(page)
    db.commit()
    db.refresh(website)
    db.refresh(page)
    return website, page


class TestAuthorizationBoundary:
    def test_every_website_scoped_endpoint_refuses_a_stranger(self, client, db, other_site):
        website, _ = other_site
        stranger = make_user(db, email="stranger@example.com")
        headers = auth_headers(stranger)

        for path in (
            f"/api/websites/{website.id}",
            f"/api/websites/{website.id}/pages",
            f"/api/websites/{website.id}/crawls",
            f"/api/websites/{website.id}/issues",
            f"/api/websites/{website.id}/issues/summary",
            f"/api/websites/{website.id}/integrations",
            f"/api/websites/{website.id}/priority/weights",
            f"/api/websites/{website.id}/recommendations",
            f"/api/websites/{website.id}/ai/selection",
            f"/api/websites/{website.id}/jobs",
            f"/api/dashboard/websites/{website.id}",
            f"/api/dashboard/websites/{website.id}/trends",
            f"/api/websites/{website.id}/pages/1/debug",
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 404, f"{path} returned {response.status_code}"

        assert client.get(
            "/api/integrations/ga4/debug",
            params={"website_id": website.id},
            headers=headers,
        ).status_code == 404

    def test_a_foreign_page_is_not_readable(self, client, db, other_site):
        _, page = other_site
        stranger = make_user(db, email="stranger2@example.com")
        assert client.get(
            f"/api/pages/{page.id}", headers=auth_headers(stranger)
        ).status_code == 404

    def test_write_endpoints_refuse_a_stranger(self, client, db, other_site):
        website, _ = other_site
        stranger = make_user(db, email="stranger3@example.com")
        headers = auth_headers(stranger)

        assert client.post(
            f"/api/websites/{website.id}/crawls", json={"mode": "full"}, headers=headers
        ).status_code == 404
        assert client.patch(
            f"/api/websites/{website.id}", json={"name": "Hijacked"}, headers=headers
        ).status_code == 404
        assert client.delete(f"/api/websites/{website.id}", headers=headers).status_code == 404

    def test_not_found_is_used_instead_of_forbidden(self, client, db, other_site):
        """403 would confirm the id exists; 404 reveals nothing."""
        website, _ = other_site
        stranger = make_user(db, email="stranger4@example.com")

        real = client.get(f"/api/websites/{website.id}", headers=auth_headers(stranger))
        fake = client.get("/api/websites/999999", headers=auth_headers(stranger))
        assert real.status_code == fake.status_code == 404

    def test_a_viewer_can_read_but_not_write(self, client, db, other_site):
        website, _ = other_site
        viewer = make_user(db, email="viewer@other.example.com", role=UserRole.VIEWER)
        db.add(
            WebsiteMember(website_id=website.id, user_id=viewer.id, role=MemberRole.VIEWER)
        )
        db.commit()
        headers = auth_headers(viewer)

        assert client.get(f"/api/websites/{website.id}", headers=headers).status_code == 200
        assert client.patch(
            f"/api/websites/{website.id}", json={"name": "No"}, headers=headers
        ).status_code == 403

    def test_admin_only_endpoints_reject_members(self, client, member_user, admin_user):
        for path in ("/api/settings", "/api/settings/priority/weights", "/api/system/health"):
            assert client.get(
                path, headers=auth_headers(member_user)
            ).status_code == 403
            assert client.get(path, headers=auth_headers(admin_user)).status_code == 200

    def test_unauthenticated_requests_are_refused_everywhere(self, client, other_site):
        website, page = other_site
        for path in (
            "/api/websites",
            f"/api/websites/{website.id}",
            f"/api/pages/{page.id}",
            "/api/dashboard/overview",
            "/api/jobs",
            "/api/seo/rules",
            "/api/ai/providers",
        ):
            assert client.get(path).status_code == 401, path

    def test_only_health_and_auth_config_are_public(self, client):
        assert client.get("/health").status_code == 200
        assert client.get("/api/auth/config").status_code == 200


# ── Credential confidentiality ──────────────────────────────────────────────


class TestCredentialConfidentiality:
    SECRETS = {
        "gsc": "ya29.a0AfH6SMB-google-access-token",
        "semrush": "semrush-live-api-key-abcdef",
        "github": "github-webhook-shared-secret",
    }

    @pytest.fixture
    def site_with_secrets(self, db, member_user):
        website = Website(name="Acme", url="https://acme.test/", domain="acme.test")
        db.add(website)
        db.flush()
        db.add(
            WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER)
        )
        db.commit()

        upsert_integration(
            db, website, IntegrationProvider.GSC,
            credentials={"refresh_token": self.SECRETS["gsc"], "access_token": self.SECRETS["gsc"]},
            config={"site_url": "sc-domain:acme.test"},
        )
        upsert_integration(
            db, website, IntegrationProvider.SEMRUSH,
            credentials={"api_key": self.SECRETS["semrush"]},
        )
        upsert_integration(
            db, website, IntegrationProvider.GITHUB,
            credentials={"webhook_secret": self.SECRETS["github"]},
        )
        db.refresh(website)
        return website

    def test_nothing_is_stored_in_plaintext(self, db, site_with_secrets):
        for integration in db.query(Integration).all():
            blob = integration.credentials_encrypted
            assert blob
            for secret in self.SECRETS.values():
                assert secret not in blob

    def test_no_endpoint_returns_a_credential(self, client, db, site_with_secrets, member_user):
        headers = auth_headers(member_user)
        for path in (
            f"/api/websites/{site_with_secrets.id}",
            f"/api/websites/{site_with_secrets.id}/integrations",
            "/api/websites",
            "/api/dashboard/overview",
            f"/api/dashboard/websites/{site_with_secrets.id}",
        ):
            body = client.get(path, headers=headers).text
            for secret in self.SECRETS.values():
                assert secret not in body, f"{path} leaked a credential"
            assert "credentials_encrypted" not in body

    def test_the_openapi_schema_exposes_no_credential_field(self, client):
        schema = client.get("/openapi.json").json()
        integration_schema = schema["components"]["schemas"].get("IntegrationResponse", {})
        assert "credentials_encrypted" not in integration_schema.get("properties", {})

    def test_a_credential_blob_is_opaque_without_the_key(self, monkeypatch):
        from app.core import crypto

        ciphertext = encrypt_json({"api_key": "top-secret"})
        monkeypatch.setattr(crypto.settings, "secret_key", "an-entirely-different-secret")
        with pytest.raises(crypto.CredentialDecryptionError):
            crypto.decrypt_json(ciphertext)


# ── Log redaction ───────────────────────────────────────────────────────────


class TestLogRedaction:
    @pytest.mark.parametrize(
        "line,secret",
        [
            ("GET https://api.semrush.com/?key=live-key-9876&type=url_organic", "live-key-9876"),
            ("Authorization: Bearer ya29.a0AfH6SMBlongtokenvalue", "ya29.a0AfH6SMBlongtokenvalue"),
            ('{"refresh_token": "1//04abcdefghijklmno"}', "1//04abcdefghijklmno"),
            ("client_secret=GOCSPX-abcdefghijklmnop", "GOCSPX-abcdefghijklmnop"),
            ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
            ("api_key=sk-proj-abcdefghijklmnopqrst", "sk-proj-abcdefghijklmnopqrst"),
            ("webhook_secret: my-shared-webhook-secret", "my-shared-webhook-secret"),
        ],
    )
    def test_credentials_never_survive_redaction(self, line, secret):
        assert secret not in redact(line)

    def test_the_filter_is_installed_on_the_root_logger(self):
        from app.core.logging import configure_logging

        configure_logging()
        root = logging.getLogger()
        assert root.handlers
        assert any(
            any(isinstance(f, RedactingFilter) for f in handler.filters)
            for handler in root.handlers
        )

    def test_a_real_log_call_is_scrubbed(self, caplog):
        from app.core.logging import configure_logging

        configure_logging()
        logger = logging.getLogger("test.security")
        for handler in logging.getLogger().handlers:
            for log_filter in handler.filters:
                if isinstance(log_filter, RedactingFilter):
                    logger.addFilter(log_filter)

        with caplog.at_level(logging.INFO):
            logger.info("calling provider with api_key=live-secret-value-123")

        assert "live-secret-value-123" not in caplog.text

    def test_ordinary_messages_are_untouched(self):
        message = "Crawl run 42 completed: 1200 pages, avg score 87.3"
        assert redact(message) == message


# ── Input validation ────────────────────────────────────────────────────────


class TestInputValidation:
    def test_a_malformed_website_url_is_rejected(self, client, member_user):
        for url in ("not-a-url", "javascript:alert(1)", "", "ftp://example.com"):
            response = client.post(
                "/api/websites",
                json={"name": "Bad", "url": url},
                headers=auth_headers(member_user),
            )
            assert response.status_code == 422, url

    def test_pagination_bounds_are_enforced(self, client, db, member_user, website):
        headers = auth_headers(member_user)
        assert client.get(
            f"/api/websites/{website.id}/pages?limit=99999", headers=headers
        ).status_code == 422
        assert client.get(
            f"/api/websites/{website.id}/pages?offset=-1", headers=headers
        ).status_code == 422

    def test_error_responses_carry_no_stack_trace(self, client, member_user):
        body = client.get("/api/websites/999999", headers=auth_headers(member_user)).json()
        assert set(body) == {"error"}
        assert "Traceback" not in str(body)
        assert "File \"" not in str(body)

    def test_validation_errors_name_the_offending_field(self, client, member_user):
        body = client.post(
            "/api/websites", json={"name": "x"}, headers=auth_headers(member_user)
        ).json()
        assert body["error"]["code"] == "validation_error"
        assert any("url" in detail["field"] for detail in body["error"]["details"])


class TestProductionGuards:
    def test_the_app_refuses_to_start_with_the_development_secret(self, monkeypatch):
        """A default SECRET_KEY in production would make every token forgeable."""
        import asyncio

        from app.main import lifespan

        monkeypatch.setattr("app.main.settings.environment", "production")
        monkeypatch.setattr("app.main.settings.secret_key", "dev-only-insecure-secret-change-me")

        async def run():
            async with lifespan(None):
                pass

        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            asyncio.run(run())

    def test_the_app_refuses_to_start_with_the_default_admin_password(self, monkeypatch):
        """A default bootstrap admin password in production would let anyone log in as admin."""
        import asyncio

        from app.main import lifespan

        monkeypatch.setattr("app.main.settings.environment", "production")
        monkeypatch.setattr("app.main.settings.secret_key", "a-properly-random-production-secret")
        monkeypatch.setattr("app.main.settings.bootstrap_admin_password", "password123")

        async def run():
            async with lifespan(None):
                pass

        with pytest.raises(RuntimeError, match="BOOTSTRAP_ADMIN_PASSWORD"):
            asyncio.run(run())

    def test_a_configured_secret_allows_startup(self, monkeypatch):
        import asyncio

        from app.main import lifespan

        monkeypatch.setattr("app.main.settings.environment", "production")
        monkeypatch.setattr("app.main.settings.secret_key", "a-properly-random-production-secret")
        monkeypatch.setattr("app.main.settings.bootstrap_admin_email", "")
        monkeypatch.setattr("app.main.settings.bootstrap_admin_password", "a-properly-random-admin-password")

        async def run():
            async with lifespan(None):
                return True

        assert asyncio.run(run()) is True


class TestWebsiteDetailWithIntegrations:
    """`GET /api/websites/{id}` once failed whenever a website had any integration row."""

    def test_the_detail_endpoint_works_with_connected_integrations(
        self, client, db, member_user
    ):
        website = Website(name="Acme", url="https://acme.test/", domain="acme.test")
        db.add(website)
        db.flush()
        db.add(
            WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER)
        )
        db.commit()
        upsert_integration(
            db, website, IntegrationProvider.SEMRUSH, credentials={"api_key": "k"}
        )

        response = client.get(
            f"/api/websites/{website.id}", headers=auth_headers(member_user)
        )
        assert response.status_code == 200
        integrations = {i["provider"]: i["status"] for i in response.json()["integrations"]}
        # Every provider is reported, connected or not.
        assert integrations["semrush"] == "connected"
        assert integrations["gsc"] == "not_connected"
        assert len(integrations) == 4
