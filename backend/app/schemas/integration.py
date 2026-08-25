"""Integration schemas.

No schema in this module exposes a credential: connect requests take secrets in, responses only
ever return status and non-sensitive configuration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .common import ORMModel


class IntegrationResponse(ORMModel):
    id: int
    website_id: int
    provider: str
    status: str
    account_label: str | None
    config: dict[str, Any] | None
    connected_at: datetime | None
    token_expires_at: datetime | None
    last_sync_at: datetime | None
    last_sync_status: str | None
    last_error: str | None
    sync_count: int


class OAuthStartResponse(BaseModel):
    authorization_url: str
    provider: str
    website_id: int


class GoogleCallbackResult(BaseModel):
    provider: str
    website_id: int
    status: str
    account_label: str | None = None
    #: Properties the account can access, so the UI can prompt for a selection.
    available_targets: list[dict[str, Any]] = []
    selected_target: str | None = None
    message: str


class SelectGSCPropertyRequest(BaseModel):
    site_url: str = Field(
        min_length=1,
        description='Search Console property, e.g. "sc-domain:example.com" or "https://example.com/".',
    )


class SelectGA4PropertyRequest(BaseModel):
    property_id: str = Field(min_length=1, description='GA4 numeric property id, e.g. "412345678".')


class ServiceAccountConnectRequest(BaseModel):
    """Connect Search Console or GA4 without the browser consent flow.

    Paste the JSON key downloaded from Google Cloud. It is validated, exchanged for a live token
    immediately, and stored encrypted — the key itself is never returned by any endpoint.
    """

    key: dict[str, Any] | str = Field(
        description="The service-account JSON key, as an object or a raw JSON string."
    )
    site_url: str | None = Field(
        default=None,
        description='Search Console property, e.g. "sc-domain:example.com". Auto-detected if omitted.',
    )
    property_id: str | None = Field(
        default=None,
        description="GA4 numeric property id. Auto-detected when the account sees exactly one.",
    )


class SemrushConnectRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)
    database: str = Field(default="us", max_length=10)
    max_pages: int = Field(default=250, ge=1, le=5000)


class GitHubConnectRequest(BaseModel):
    repo: str = Field(min_length=3, max_length=255, description='"owner/repo".')
    branch: str = Field(default="main", max_length=255)
    webhook_secret: str = Field(
        min_length=8,
        max_length=255,
        description="Shared secret configured on the GitHub webhook.",
    )
    framework: str | None = Field(default=None, max_length=50)


class SyncRequest(BaseModel):
    days: int | None = Field(
        default=None, ge=1, le=480, description="Lookback window; defaults to the incremental one."
    )
    backfill: bool = Field(default=False, description="Use the full historical backfill window.")


class SyncResult(BaseModel):
    provider: str
    status: str
    summary: dict[str, Any] = {}
    message: str | None = None


class KeywordOpportunity(BaseModel):
    page_id: int
    url: str
    keyword: str | None
    position: int
    volume: int
    difficulty: float | None = None
    cpc: float | None = None
