"""Google OAuth 2.0 authorization-code flow, shared by Search Console and GA4.

One consent grant covers both products, so the refresh token is stored once per (website,
provider) pair and each connector requests the scopes it needs. Tokens are refreshed lazily and
written back encrypted.

CSRF protection uses a signed, expiring ``state`` token rather than server-side session storage,
which keeps the API stateless behind a load balancer.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import jwt
from sqlalchemy.orm import Session

from ...config import settings
from ...core.errors import IntegrationError, ValidationError
from ...core.security import TokenError, create_state_token, verify_state_token
from ...models import Integration, IntegrationStatus
from .base import (
    integration_client,
    read_credentials,
    request_with_retry,
    utcnow,
    write_credentials,
)

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"

SCOPES = {
    "gsc": [
        "https://www.googleapis.com/auth/webmasters.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
    "ga4": [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
}

#: Refresh a little before actual expiry so a long sync cannot straddle the boundary.
EXPIRY_SKEW_SECONDS = 120

#: RFC 7523 grant used for service-account (server-to-server) authentication.
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Marker stored in the credential blob to distinguish the two auth modes.
SERVICE_ACCOUNT = "service_account"

REQUIRED_SERVICE_ACCOUNT_FIELDS = ("client_email", "private_key", "token_uri")


def google_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def build_authorization_url(website_id: int, provider: str, user_id: int) -> str:
    """Return the Google consent URL for one website/provider pair."""
    if not google_configured():
        raise ValidationError(
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
        )
    if provider not in SCOPES:
        raise ValidationError(f"'{provider}' is not a Google-backed provider.")

    state = create_state_token(website_id=website_id, provider=provider, user_id=user_id)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.resolved_google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES[provider]),
        # offline + consent guarantees a refresh token even on a repeat authorisation.
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def parse_state(state: str) -> dict[str, Any]:
    """Validate the signed state returned by Google."""
    try:
        claims = verify_state_token(state)
    except TokenError as exc:
        raise ValidationError(f"OAuth state is invalid or expired: {exc}") from exc

    if not claims.get("website_id") or not claims.get("provider"):
        raise ValidationError("OAuth state is missing the website or provider claim.")
    return claims


async def exchange_code(code: str) -> dict[str, Any]:
    """Swap an authorization code for access and refresh tokens."""
    async with integration_client() as client:
        response = await request_with_retry(
            client,
            "POST",
            TOKEN_ENDPOINT,
            provider="Google",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    payload = response.json()

    if not payload.get("refresh_token"):
        raise IntegrationError(
            "Google did not return a refresh token. Revoke the app's access in your Google "
            "account and connect again so a new consent is granted.",
            code="integration_no_refresh_token",
        )
    return payload


async def fetch_account_email(access_token: str) -> str | None:
    """Best-effort account label so the UI can show which Google account is connected."""
    try:
        async with integration_client(timeout=15) as client:
            response = await request_with_retry(
                client,
                "GET",
                USERINFO_ENDPOINT,
                provider="Google",
                headers={"Authorization": f"Bearer {access_token}"},
                max_retries=1,
            )
        return response.json().get("email")
    except Exception as exc:
        logger.debug("Could not read the Google account email: %s", exc)
        return None


def credentials_from_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise Google's token response into the stored credential blob."""
    expires_at = utcnow() + timedelta(seconds=int(payload.get("expires_in", 3600)))
    return {
        "refresh_token": payload["refresh_token"],
        "access_token": payload.get("access_token", ""),
        "expires_at": expires_at.isoformat(),
        "scope": payload.get("scope", ""),
        "token_type": payload.get("token_type", "Bearer"),
    }


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a new access token."""
    async with integration_client() as client:
        response = await request_with_retry(
            client,
            "POST",
            TOKEN_ENDPOINT,
            provider="Google",
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
        )
    return response.json()



# ── Service accounts (server-to-server) ─────────────────────────────────────
#
# The authorization-code flow above needs a human at a browser and, while the consent screen is
# unverified, Google expires its refresh tokens after seven days. A service account has neither
# problem: it authenticates with a signed assertion, so it suits scheduled syncs far better. The
# trade-off is that access is granted per-property inside Search Console and Analytics rather than
# by consent, which is why `verify_service_account` checks reachability immediately.


def parse_service_account_key(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Validate a downloaded service-account JSON key and reduce it to what we store."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "That is not valid JSON. Paste the whole key file downloaded from Google Cloud."
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise ValidationError("The service-account key must be a JSON object.")

    if data.get("type") != SERVICE_ACCOUNT:
        raise ValidationError(
            'The key file is not a service account (its "type" should be "service_account"). '
            "Create one under IAM & Admin -> Service Accounts -> Keys -> Add key -> JSON."
        )

    missing = [f for f in REQUIRED_SERVICE_ACCOUNT_FIELDS if not data.get(f)]
    if missing:
        raise ValidationError(f"The key file is missing: {', '.join(missing)}.")

    if "-----BEGIN" not in data["private_key"]:
        raise ValidationError(
            "The private_key field does not look like a PEM key. If you pasted the JSON through "
            "a shell, its newlines may have been mangled."
        )

    return {
        "type": SERVICE_ACCOUNT,
        "client_email": data["client_email"],
        "private_key": data["private_key"],
        "private_key_id": data.get("private_key_id", ""),
        "project_id": data.get("project_id", ""),
        "token_uri": data.get("token_uri", TOKEN_ENDPOINT),
    }


def is_service_account(credentials: dict[str, Any]) -> bool:
    return credentials.get("type") == SERVICE_ACCOUNT


def _build_assertion(credentials: dict[str, Any], scopes: list[str]) -> str:
    """Sign the RFC 7523 JWT that is exchanged for an access token."""
    now = int(time.time())
    payload = {
        "iss": credentials["client_email"],
        "scope": " ".join(scopes),
        "aud": credentials.get("token_uri", TOKEN_ENDPOINT),
        "iat": now,
        # Google caps assertion lifetime at one hour.
        "exp": now + 3600,
    }
    headers = {}
    if credentials.get("private_key_id"):
        headers["kid"] = credentials["private_key_id"]

    try:
        return jwt.encode(
            payload, credentials["private_key"], algorithm="RS256", headers=headers
        )
    except Exception as exc:
        raise IntegrationError(
            "The service-account private key could not be used to sign a request. "
            "Re-download the key file from Google Cloud.",
            code="integration_bad_service_account_key",
        ) from exc


async def mint_service_account_token(
    credentials: dict[str, Any], provider: str
) -> dict[str, Any]:
    """Exchange a signed assertion for an access token."""
    if provider not in SCOPES:
        raise ValidationError(f"'{provider}' is not a Google-backed provider.")

    assertion = _build_assertion(credentials, SCOPES[provider])
    async with integration_client() as client:
        response = await request_with_retry(
            client,
            "POST",
            credentials.get("token_uri", TOKEN_ENDPOINT),
            provider="Google",
            data={"grant_type": JWT_BEARER_GRANT, "assertion": assertion},
        )
    return response.json()


async def verify_service_account(credentials: dict[str, Any], provider: str) -> str:
    """Mint a token immediately so a bad key or missing grant fails at connect time."""
    try:
        payload = await mint_service_account_token(credentials, provider)
    except IntegrationError as exc:
        raise IntegrationError(
            f"Google rejected the service account: {exc.message} "
            "Check that the key is current and that the Cloud project has the APIs enabled.",
            code="integration_service_account_rejected",
        ) from exc

    token = payload.get("access_token")
    if not token:
        raise IntegrationError("Google returned no access token for the service account.")
    return token


async def get_access_token(db: Session, integration: Integration) -> str:
    """Return a valid access token, refreshing and persisting it when needed."""
    credentials = read_credentials(integration)
    access_token = credentials.get("access_token")
    expires_at_raw = credentials.get("expires_at")

    if access_token and expires_at_raw:
        from datetime import datetime

        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=utcnow().tzinfo)
            if (expires_at - utcnow()).total_seconds() > EXPIRY_SKEW_SECONDS:
                return access_token
        except ValueError:
            pass  # malformed timestamp: fall through and refresh

    if is_service_account(credentials):
        # No refresh token exists; a fresh assertion is signed and exchanged instead.
        payload = await mint_service_account_token(credentials, integration.provider)
        expires_in = int(payload.get("expires_in", 3600))
        credentials["access_token"] = payload.get("access_token", "")
        credentials["expires_at"] = (utcnow() + timedelta(seconds=expires_in)).isoformat()
        write_credentials(db, integration, credentials)
        integration.token_expires_at = utcnow() + timedelta(seconds=expires_in)
        db.commit()
        logger.info(
            "Minted a service-account access token for integration %s.", integration.id
        )
        return credentials["access_token"]

    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        integration.status = IntegrationStatus.EXPIRED
        db.commit()
        raise IntegrationError(
            "The stored Google credentials have no refresh token. Reconnect the integration.",
            code="integration_reauthorisation_required",
        )

    payload = await refresh_access_token(refresh_token)
    credentials["access_token"] = payload.get("access_token", "")
    credentials["expires_at"] = (
        utcnow() + timedelta(seconds=int(payload.get("expires_in", 3600)))
    ).isoformat()
    # Google usually omits the refresh token on refresh; keep the existing one when it does.
    if payload.get("refresh_token"):
        credentials["refresh_token"] = payload["refresh_token"]

    write_credentials(db, integration, credentials)
    integration.token_expires_at = utcnow() + timedelta(
        seconds=int(payload.get("expires_in", 3600))
    )
    db.commit()

    logger.info("Refreshed the Google access token for integration %s.", integration.id)
    return credentials["access_token"]
