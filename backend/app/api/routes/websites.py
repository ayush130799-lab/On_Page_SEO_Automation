"""Website onboarding and management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.deps import (
    CurrentUser,
    DbSession,
    ReadableWebsite,
    WritableWebsite,
    accessible_website_ids,
)
from ...core.errors import ConflictError
from ...models import (
    Integration,
    IntegrationProvider,
    MemberRole,
    User,
    UserRole,
    Website,
    WebsiteMember,
)
from ...schemas.common import MessageResponse, Page
from ...schemas.website import (
    IntegrationStatusSummary,
    WebsiteCreate,
    WebsiteDetailResponse,
    WebsiteResponse,
    WebsiteUpdate,
)
from ...utils.url_utils import domain_of, normalize_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/websites", tags=["websites"])

ALL_PROVIDERS = [
    IntegrationProvider.GSC,
    IntegrationProvider.GA4,
    IntegrationProvider.SEMRUSH,
    IntegrationProvider.GITHUB,
]


def _integration_summaries(db: Session, website_id: int) -> list[IntegrationStatusSummary]:
    """Status for every supported provider, including ones never connected."""
    existing = {
        integration.provider: integration
        for integration in db.query(Integration)
        .filter(Integration.website_id == website_id)
        .all()
    }
    summaries = []
    for provider in ALL_PROVIDERS:
        integration = existing.get(provider)
        summaries.append(
            IntegrationStatusSummary(
                provider=provider,
                status=integration.status if integration else "not_connected",
                account_label=integration.account_label if integration else None,
                last_sync_at=integration.last_sync_at if integration else None,
                last_error=integration.last_error if integration else None,
            )
        )
    return summaries


def _detail(db: Session, website: Website) -> WebsiteDetailResponse:
    """Build the detail response.

    Constructed field-by-field rather than by validating the ORM object directly: the schema's
    ``integrations`` field holds status summaries for *every* provider, including ones never
    connected, while ``Website.integrations`` is the relationship holding only the rows that
    exist. Validating the ORM object would try to coerce those rows into summaries and fail.
    """
    return WebsiteDetailResponse(
        **WebsiteResponse.model_validate(website).model_dump(),
        github_path_map=website.github_path_map,
        integrations=_integration_summaries(db, website.id),
    )


@router.post("", response_model=WebsiteDetailResponse, status_code=status.HTTP_201_CREATED)
def create_website(payload: WebsiteCreate, user: CurrentUser, db: DbSession):
    """Onboard a website. The creator is granted owner membership automatically."""
    url = normalize_url(str(payload.url))
    domain = domain_of(url)

    if db.query(Website).filter(func.lower(Website.url) == url.lower()).first():
        raise ConflictError(f"A website with the URL {url} already exists.")

    website = Website(
        name=payload.name.strip(),
        url=url,
        domain=domain,
        created_by_id=user.id,
        github_repo=payload.github_repo,
        github_branch=payload.github_branch,
        github_framework=payload.github_framework,
        github_path_map=payload.github_path_map,
        max_pages=payload.max_pages,
        render_mode=payload.render_mode,
        crawl_delay=payload.crawl_delay,
        respect_robots_txt=payload.respect_robots_txt,
        include_patterns=payload.include_patterns,
        exclude_patterns=payload.exclude_patterns,
    )
    db.add(website)
    db.flush()

    db.add(WebsiteMember(website_id=website.id, user_id=user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(website)
    logger.info("Website %s (%s) onboarded by user %s", website.id, website.domain, user.id)
    return _detail(db, website)


@router.get("", response_model=Page[WebsiteResponse])
def list_websites(
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str | None = None,
    is_active: bool | None = None,
):
    """List every website the caller can see (the company portfolio)."""
    stmt = select(Website)
    allowed = accessible_website_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Website.id.in_(allowed or [-1]))
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(Website.name.ilike(pattern) | Website.domain.ilike(pattern))
    if is_active is not None:
        stmt = stmt.where(Website.is_active.is_(is_active))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Website.name.asc()).limit(limit).offset(offset)
    ).all()
    return Page[WebsiteResponse](
        total=total,
        limit=limit,
        offset=offset,
        items=[WebsiteResponse.model_validate(row) for row in rows],
    )


@router.get("/{website_id}", response_model=WebsiteDetailResponse)
def get_website(website: ReadableWebsite, db: DbSession):
    return _detail(db, website)


@router.patch("/{website_id}", response_model=WebsiteDetailResponse)
def update_website(payload: WebsiteUpdate, website: WritableWebsite, db: DbSession):
    updates = payload.model_dump(exclude_unset=True)
    if "url" in updates and updates["url"] is not None:
        updates["url"] = normalize_url(str(updates["url"]))
        updates["domain"] = domain_of(updates["url"])
    for field, value in updates.items():
        setattr(website, field, value)
    db.commit()
    db.refresh(website)
    return _detail(db, website)


@router.delete("/{website_id}", response_model=MessageResponse)
def delete_website(website: WritableWebsite, user: CurrentUser, db: DbSession):
    """Delete a website and every record that hangs off it."""
    if user.role != UserRole.ADMIN:
        member = (
            db.query(WebsiteMember)
            .filter(
                WebsiteMember.website_id == website.id,
                WebsiteMember.user_id == user.id,
                WebsiteMember.role == MemberRole.OWNER,
            )
            .first()
        )
        if member is None:
            from ...core.errors import AuthorizationError

            raise AuthorizationError("Only an owner or administrator can delete a website.")

    name = website.name
    db.delete(website)
    db.commit()
    logger.info("Website %s (%s) deleted by user %s", website.id, name, user.id)
    return MessageResponse(message=f"Website '{name}' was deleted.")


@router.post("/{website_id}/members", response_model=MessageResponse)
def add_member(
    website: WritableWebsite,
    db: DbSession,
    email: str = Query(..., description="Email of an existing user to grant access to."),
    role: MemberRole = Query(MemberRole.EDITOR),
):
    """Grant an existing user access to this website."""
    target = db.query(User).filter(User.email == email.lower().strip()).first()
    if target is None:
        from ...core.errors import NotFoundError

        raise NotFoundError(f"No user found with email {email}.")

    existing = (
        db.query(WebsiteMember)
        .filter(WebsiteMember.website_id == website.id, WebsiteMember.user_id == target.id)
        .first()
    )
    if existing:
        existing.role = role
    else:
        db.add(WebsiteMember(website_id=website.id, user_id=target.id, role=role))
    db.commit()
    return MessageResponse(message=f"{target.email} now has {role} access to {website.name}.")
