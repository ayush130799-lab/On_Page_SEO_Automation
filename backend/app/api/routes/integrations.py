"""Integration management: connect, configure, sync and disconnect."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import RedirectResponse

from ...config import settings
from ...core.deps import CurrentUser, DbSession, ReadableWebsite, WritableWebsite
from ...core.errors import NotFoundError, ValidationError
from ...core.ratelimit import default_rate_limit
from ...db import SessionLocal
from ...models import Integration, IntegrationProvider, IntegrationStatus, Website
from ...schemas.common import MessageResponse
from ...schemas.integration import (
    GitHubConnectRequest,
    GoogleCallbackResult,
    IntegrationResponse,
    KeywordOpportunity,
    OAuthStartResponse,
    SelectGA4PropertyRequest,
    SelectGSCPropertyRequest,
    SemrushConnectRequest,
    SyncRequest,
    SyncResult,
)
from ...services.integrations import ga4, google_oauth, gsc, semrush
from ...services.integrations.base import (
    disconnect as disconnect_integration,
    get_integration,
    require_integration,
    upsert_integration,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["integrations"])

GOOGLE_PROVIDERS = {IntegrationProvider.GSC, IntegrationProvider.GA4}
SYNCABLE = {IntegrationProvider.GSC, IntegrationProvider.GA4, IntegrationProvider.SEMRUSH}


# ── Status ──────────────────────────────────────────────────────────────────


@router.get("/websites/{website_id}/integrations", response_model=list[IntegrationResponse])
def list_integrations(website: ReadableWebsite, db: DbSession):
    """Configured integrations for a website. Credentials are never included."""
    return db.query(Integration).filter(Integration.website_id == website.id).all()


# ── Google OAuth ────────────────────────────────────────────────────────────


@router.post(
    "/websites/{website_id}/integrations/{provider}/authorize",
    response_model=OAuthStartResponse,
)
def start_google_authorization(
    provider: str, website: WritableWebsite, user: CurrentUser, db: DbSession
):
    """Begin the Google consent flow for Search Console or GA4."""
    if provider not in GOOGLE_PROVIDERS:
        raise ValidationError(f"'{provider}' does not use Google OAuth.")

    # Record the pending integration so the UI can show it before consent completes.
    if get_integration(db, website.id, provider) is None:
        db.add(Integration(website_id=website.id, provider=provider))
        db.commit()

    return OAuthStartResponse(
        authorization_url=google_oauth.build_authorization_url(website.id, provider, user.id),
        provider=provider,
        website_id=website.id,
    )


@router.get("/integrations/google/callback")
async def google_callback(
    db: DbSession,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
):
    """OAuth redirect target.

    Google calls this directly in the user's browser, so it cannot require a bearer token — the
    signed ``state`` parameter is what authenticates the request. On success the browser is sent
    back to the frontend with the outcome in the query string.
    """
    if error:
        return RedirectResponse(
            f"{settings.frontend_base_url}/integrations/callback"
            f"?status=error&message={error}",
            status_code=302,
        )
    if not code or not state:
        raise ValidationError("The OAuth callback is missing its code or state parameter.")

    claims = google_oauth.parse_state(state)
    website_id = int(claims["website_id"])
    provider = str(claims["provider"])

    website = db.get(Website, website_id)
    if website is None:
        raise NotFoundError(f"Website {website_id} no longer exists.")

    token_payload = await google_oauth.exchange_code(code)
    credentials = google_oauth.credentials_from_token_response(token_payload)
    account_email = await google_oauth.fetch_account_email(credentials["access_token"])

    integration = upsert_integration(
        db,
        website,
        provider,
        credentials=credentials,
        account_label=account_email,
        status=IntegrationStatus.CONNECTED,
    )

    # Auto-select the target when it can be determined unambiguously.
    selected: str | None = None
    targets: list[dict] = []
    try:
        if provider == IntegrationProvider.GSC:
            detected = await gsc.detect_site_url(credentials["access_token"], website)
            entries = await gsc.list_sites(credentials["access_token"])
            targets = [
                {"id": e.get("siteUrl"), "label": e.get("siteUrl"),
                 "permission": e.get("permissionLevel")}
                for e in entries
            ]
            if detected:
                integration.config = {**(integration.config or {}), "site_url": detected}
                selected = detected
        else:
            properties = await ga4.list_properties(credentials["access_token"])
            targets = [
                {"id": p["property_id"], "label": p["display_name"], "account": p["account"]}
                for p in properties
            ]
            if len(properties) == 1:
                integration.config = {
                    **(integration.config or {}),
                    "property_id": properties[0]["property_id"],
                }
                selected = properties[0]["property_id"]
        db.commit()
    except Exception as exc:
        # Consent succeeded even if discovery did not; the user can pick the property manually.
        logger.warning("Post-authorisation discovery failed for %s: %s", provider, exc)

    result = GoogleCallbackResult(
        provider=provider,
        website_id=website_id,
        status=integration.status,
        account_label=account_email,
        available_targets=targets,
        selected_target=selected,
        message=(
            f"{provider.upper()} connected."
            if selected
            else f"{provider.upper()} connected — select a property to finish setup."
        ),
    )

    return RedirectResponse(
        f"{settings.frontend_base_url}/websites/{website_id}/integrations"
        f"?status=connected&provider={provider}"
        f"&selected={selected or ''}",
        status_code=302,
    ) if settings.frontend_base_url else result


@router.get("/websites/{website_id}/integrations/gsc/properties")
async def list_gsc_properties(website: ReadableWebsite, db: DbSession):
    """Search Console properties the connected account can read."""
    integration = require_integration(db, website.id, IntegrationProvider.GSC)
    access_token = await google_oauth.get_access_token(db, integration)
    entries = await gsc.list_sites(access_token)
    return {
        "selected": (integration.config or {}).get("site_url"),
        "properties": [
            {"site_url": e.get("siteUrl"), "permission_level": e.get("permissionLevel")}
            for e in entries
        ],
    }


@router.put("/websites/{website_id}/integrations/gsc/property", response_model=IntegrationResponse)
def select_gsc_property(
    payload: SelectGSCPropertyRequest, website: WritableWebsite, db: DbSession
):
    integration = require_integration(db, website.id, IntegrationProvider.GSC)
    integration.config = {**(integration.config or {}), "site_url": payload.site_url}
    db.commit()
    db.refresh(integration)
    return integration


@router.get("/websites/{website_id}/integrations/ga4/properties")
async def list_ga4_properties(website: ReadableWebsite, db: DbSession):
    """GA4 properties the connected account can read."""
    integration = require_integration(db, website.id, IntegrationProvider.GA4)
    access_token = await google_oauth.get_access_token(db, integration)
    return {
        "selected": (integration.config or {}).get("property_id"),
        "properties": await ga4.list_properties(access_token),
    }


@router.put("/websites/{website_id}/integrations/ga4/property", response_model=IntegrationResponse)
def select_ga4_property(
    payload: SelectGA4PropertyRequest, website: WritableWebsite, db: DbSession
):
    integration = require_integration(db, website.id, IntegrationProvider.GA4)
    integration.config = {**(integration.config or {}), "property_id": payload.property_id}
    db.commit()
    db.refresh(integration)
    return integration


# ── Semrush ─────────────────────────────────────────────────────────────────


@router.post("/websites/{website_id}/integrations/semrush", response_model=IntegrationResponse)
async def connect_semrush(
    payload: SemrushConnectRequest, website: WritableWebsite, db: DbSession
):
    """Store a Semrush API key after verifying it against the live API."""
    verification = await semrush.verify_api_key(payload.api_key)

    return upsert_integration(
        db,
        website,
        IntegrationProvider.SEMRUSH,
        credentials={"api_key": payload.api_key},
        config={
            "database": payload.database,
            "max_pages": payload.max_pages,
            "api_units_remaining": verification["api_units_remaining"],
        },
        account_label=f"Semrush key {verification['key_hint']}",
        status=IntegrationStatus.CONNECTED,
    )


@router.get(
    "/websites/{website_id}/integrations/semrush/opportunities",
    response_model=list[KeywordOpportunity],
)
def list_keyword_opportunities(
    website: ReadableWebsite, db: DbSession, limit: int = Query(50, ge=1, le=500)
):
    """Striking-distance keywords (positions 4-20) across the site."""
    return semrush.keyword_opportunities(db, website.id, limit)


# ── GitHub ──────────────────────────────────────────────────────────────────


@router.post("/websites/{website_id}/integrations/github", response_model=IntegrationResponse)
def connect_github(payload: GitHubConnectRequest, website: WritableWebsite, db: DbSession):
    """Map a repository to this website and store the webhook secret encrypted."""
    repo = payload.repo.strip()
    if repo.startswith("http"):
        repo = repo.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    if repo.count("/") != 1:
        raise ValidationError('The repository must be given as "owner/repo".')

    website.github_repo = repo
    website.github_branch = payload.branch
    if payload.framework:
        website.github_framework = payload.framework
    db.commit()

    return upsert_integration(
        db,
        website,
        IntegrationProvider.GITHUB,
        credentials={"webhook_secret": payload.webhook_secret},
        config={
            "repo": repo,
            "branch": payload.branch,
            "webhook_url": f"{settings.public_base_url}/api/webhooks/github",
        },
        account_label=repo,
        status=IntegrationStatus.CONNECTED,
    )


# ── Sync ────────────────────────────────────────────────────────────────────


def _run_sync(website_id: int, provider: str, days: int | None) -> None:
    """Execute a provider sync in its own session (background task entry point)."""
    db = SessionLocal()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return
        module = {
            IntegrationProvider.GSC: gsc,
            IntegrationProvider.GA4: ga4,
        }.get(provider)
        if module is not None:
            asyncio.run(module.sync(db, website, days=days))
        elif provider == IntegrationProvider.SEMRUSH:
            asyncio.run(semrush.sync(db, website))
    except Exception as exc:
        logger.exception("Background %s sync failed for website %s: %s", provider, website_id, exc)
    finally:
        db.close()


def dispatch_sync(
    website_id: int, provider: str, days: int | None, background_tasks: BackgroundTasks | None
) -> str:
    """Queue a sync on Celery when available, otherwise run it as a background task."""
    if settings.use_celery:
        try:
            from ...services.jobs.tasks import run_sync_task

            run_sync_task.delay(website_id, provider, days)
            return "celery"
        except Exception as exc:
            logger.warning("Celery dispatch failed for the %s sync: %s", provider, exc)

    if background_tasks is not None:
        background_tasks.add_task(_run_sync, website_id, provider, days)
        return "background_task"

    _run_sync(website_id, provider, days)
    return "inline"


@router.post(
    "/websites/{website_id}/integrations/{provider}/sync",
    response_model=SyncResult,
    dependencies=[Depends(default_rate_limit)],
)
def trigger_sync(
    provider: str,
    payload: SyncRequest,
    website: WritableWebsite,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Queue a metric sync for one provider."""
    if provider not in SYNCABLE:
        raise ValidationError(f"'{provider}' does not support syncing.")

    integration = require_integration(db, website.id, provider)
    if integration.status == IntegrationStatus.NOT_CONNECTED:
        raise ValidationError(f"The {provider} integration is not connected.")

    days = (
        settings.integration_sync_backfill_days
        if payload.backfill
        else payload.days
    )
    transport = dispatch_sync(website.id, provider, days, background_tasks)

    return SyncResult(
        provider=provider,
        status="queued",
        summary={"transport": transport, "days": days},
        message=f"A {provider} sync has been queued.",
    )


@router.post(
    "/websites/{website_id}/integrations/sync-all",
    response_model=list[SyncResult],
    dependencies=[Depends(default_rate_limit)],
)
def trigger_all_syncs(
    payload: SyncRequest,
    website: WritableWebsite,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    """Queue a sync for every connected provider on this website."""
    results = []
    for provider in SYNCABLE:
        integration = get_integration(db, website.id, provider)
        if integration is None or not integration.is_connected:
            results.append(
                SyncResult(provider=provider, status="skipped", message="Not connected.")
            )
            continue
        days = settings.integration_sync_backfill_days if payload.backfill else payload.days
        transport = dispatch_sync(website.id, provider, days, background_tasks)
        results.append(
            SyncResult(
                provider=provider,
                status="queued",
                summary={"transport": transport, "days": days},
            )
        )
    return results


@router.delete(
    "/websites/{website_id}/integrations/{provider}", response_model=MessageResponse
)
def disconnect(provider: str, website: WritableWebsite, db: DbSession):
    """Forget a provider's credentials. Collected metrics are retained deliberately."""
    integration = require_integration(db, website.id, provider)
    disconnect_integration(db, integration)
    return MessageResponse(
        message=f"{provider} disconnected. Previously collected metrics were kept."
    )
