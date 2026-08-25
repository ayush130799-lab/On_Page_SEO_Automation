"""Shared integration plumbing: credential storage, status transitions and resilient HTTP.

Credentials only ever exist in plaintext inside this module's callers for the duration of a
request; at rest they are Fernet ciphertext in ``integrations.credentials_encrypted``, and they are
never placed in a log line, an API response or a job payload.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ...config import settings
from ...core.crypto import CredentialDecryptionError, decrypt_json, encrypt_json
from ...core.errors import IntegrationError, NotFoundError
from ...models import Integration, IntegrationStatus, Website

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Credential storage ──────────────────────────────────────────────────────


def get_integration(db: Session, website_id: int, provider: str) -> Integration | None:
    return (
        db.query(Integration)
        .filter(Integration.website_id == website_id, Integration.provider == provider)
        .first()
    )


def require_integration(db: Session, website_id: int, provider: str) -> Integration:
    integration = get_integration(db, website_id, provider)
    if integration is None:
        raise NotFoundError(f"The {provider} integration is not configured for this website.")
    return integration


def upsert_integration(
    db: Session,
    website: Website,
    provider: str,
    *,
    credentials: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    account_label: str | None = None,
    status: str = IntegrationStatus.CONNECTED,
    token_expires_at: datetime | None = None,
) -> Integration:
    """Create or update an integration, encrypting the credential blob on the way in."""
    integration = get_integration(db, website.id, provider)
    if integration is None:
        integration = Integration(website_id=website.id, provider=provider)
        db.add(integration)

    if credentials is not None:
        integration.credentials_encrypted = encrypt_json(credentials)
    if config is not None:
        integration.config = {**(integration.config or {}), **config}
    if account_label is not None:
        integration.account_label = account_label

    integration.status = status
    integration.token_expires_at = token_expires_at
    integration.last_error = None
    if status == IntegrationStatus.CONNECTED and integration.connected_at is None:
        integration.connected_at = utcnow()

    db.commit()
    db.refresh(integration)
    return integration


def read_credentials(integration: Integration) -> dict[str, Any]:
    """Decrypt the stored credential blob."""
    if not integration.credentials_encrypted:
        raise IntegrationError(
            f"The {integration.provider} integration has no stored credentials.",
            code="integration_not_connected",
        )
    try:
        return decrypt_json(integration.credentials_encrypted)
    except CredentialDecryptionError as exc:
        raise IntegrationError(
            f"Stored {integration.provider} credentials could not be decrypted. "
            "Reconnect the integration.",
            code="integration_credentials_unreadable",
        ) from exc


def write_credentials(db: Session, integration: Integration, credentials: dict[str, Any]) -> None:
    """Persist rotated credentials (e.g. a refreshed access token)."""
    integration.credentials_encrypted = encrypt_json(credentials)
    db.commit()


def mark_sync_started(db: Session, integration: Integration) -> None:
    integration.status = IntegrationStatus.SYNCING
    integration.last_sync_status = "running"
    db.commit()


def mark_sync_success(db: Session, integration: Integration, summary: str | None = None) -> None:
    integration.status = IntegrationStatus.CONNECTED
    integration.last_sync_at = utcnow()
    integration.last_sync_status = "success"
    integration.last_error = None
    integration.sync_count += 1
    if summary:
        integration.config = {**(integration.config or {}), "last_sync_summary": summary}
    db.commit()


def mark_sync_failure(db: Session, integration: Integration, error: str) -> None:
    """Record a failure without ever storing the provider's raw response, which may echo a token."""
    from ...core.logging import redact

    integration.status = IntegrationStatus.ERROR
    integration.last_sync_at = utcnow()
    integration.last_sync_status = "failed"
    integration.last_error = redact(error)[:1000]
    db.commit()


def disconnect(db: Session, integration: Integration) -> None:
    """Forget the credentials and reset status. Historical metrics are deliberately kept."""
    integration.credentials_encrypted = None
    integration.status = IntegrationStatus.NOT_CONNECTED
    integration.connected_at = None
    integration.token_expires_at = None
    integration.account_label = None
    integration.last_error = None
    db.commit()


# ── Resilient HTTP ──────────────────────────────────────────────────────────


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    max_retries: int | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a provider request, retrying rate limits and transient server errors.

    Raises :class:`IntegrationError` with a message safe to show a user — provider bodies can echo
    the API key back, so they are never included verbatim.
    """
    attempts = max_retries or settings.integration_max_retries
    last_status: int | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            if attempt == attempts:
                raise IntegrationError(
                    f"Could not reach the {provider} API: {type(exc).__name__}."
                ) from exc
            await asyncio.sleep(min(30.0, 1.5 * (2 ** (attempt - 1))))
            continue

        if response.status_code in RETRYABLE_STATUSES and attempt < attempts:
            last_status = response.status_code
            delay = _retry_delay(response, attempt)
            logger.info(
                "%s API returned %s; retrying in %.1fs (attempt %d/%d).",
                provider, response.status_code, delay, attempt, attempts,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code == 401:
            raise IntegrationError(
                f"{provider} rejected the stored credentials. Reconnect the integration.",
                code="integration_unauthorised",
            )
        if response.status_code == 403:
            raise IntegrationError(
                f"{provider} denied access to this resource. Check the account's permissions.",
                code="integration_forbidden",
            )
        if response.status_code >= 400:
            raise IntegrationError(
                f"{provider} API returned HTTP {response.status_code}.",
                {"status_code": response.status_code},
            )
        return response

    raise IntegrationError(
        f"{provider} API is still returning HTTP {last_status} after {attempts} attempts.",
        {"status_code": last_status},
    )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return min(60.0, float(raw))
        except ValueError:
            pass
    return min(30.0, 1.5 * (2 ** (attempt - 1)))


def integration_client(timeout: int | None = None) -> httpx.AsyncClient:
    """A client configured for provider APIs (longer timeouts than the crawler uses)."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout or settings.integration_http_timeout),
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
    )
