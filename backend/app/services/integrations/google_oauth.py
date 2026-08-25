"""Google OAuth 2.0 authorization-code flow, shared by Search Console and GA4.

One consent grant covers both products, so the refresh token is stored once per (website,
provider) pair and each connector requests the scopes it needs. Tokens are refreshed lazily and
written back encrypted.

CSRF protection uses a signed, expiring ``state`` token rather than server-side session storage,
which keeps the API stateless behind a load balancer.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

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
        "redirect_uri": settings.google_redirect_uri,
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
