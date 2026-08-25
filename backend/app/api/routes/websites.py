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


@router.post("/{website_id}/seed_demo", response_model=WebsiteDetailResponse)
def seed_demo_data(website: WritableWebsite, user: CurrentUser, db: DbSession):
    """Seed realistic GSC, GA4, and Semrush demo metrics for live client demos."""
    from datetime import date, timedelta
    import random
    from ...core.errors import ValidationError
    from ...models import GA4Metric, GSCMetric, Integration, IntegrationProvider, IntegrationStatus, Page, SemrushMetric, utcnow
    from ...services.priority.engine import compute_website_priority

    pages = db.query(Page).filter(Page.website_id == website.id, Page.is_active.is_(True)).all()
    if not pages:
        raise ValidationError("Please run a crawl first so pages exist before seeding demo metrics.")

    today = date.today()

    for provider in [IntegrationProvider.GSC, IntegrationProvider.GA4, IntegrationProvider.SEMRUSH]:
        integration = (
            db.query(Integration)
            .filter(Integration.website_id == website.id, Integration.provider == provider)
            .first()
        )
        if not integration:
            integration = Integration(website_id=website.id, provider=provider)
            db.add(integration)
        integration.status = IntegrationStatus.CONNECTED
        integration.account_label = f"demo-{provider}@{website.domain}"
        integration.last_sync_at = utcnow()
        integration.last_error = None

    db.query(GSCMetric).filter(GSCMetric.website_id == website.id).delete()
    db.query(GA4Metric).filter(GA4Metric.website_id == website.id).delete()
    db.query(SemrushMetric).filter(SemrushMetric.website_id == website.id).delete()

    for i, page in enumerate(pages):
        base_traffic = random.randint(300, 3500) if i < 3 else random.randint(30, 450)
        for day_offset in range(28):
            d = today - timedelta(days=day_offset)
            factor = 0.7 + (random.random() * 0.6)
            clicks = int(base_traffic * factor)
            impressions = clicks * random.randint(12, 28)
            db.add(
                GSCMetric(
                    website_id=website.id,
                    page_id=page.id,
                    date=d,
                    clicks=clicks,
                    impressions=impressions,
                    ctr=round(clicks / max(1, impressions), 4),
                    position=round(random.uniform(1.8, 14.5), 1),
                )
            )

            users = int(clicks * random.uniform(0.85, 1.25))
            sessions = int(users * random.uniform(1.1, 1.4))
            conversions = round(users * random.uniform(0.015, 0.045), 1)
            revenue = round(conversions * random.uniform(35.0, 150.0), 2)
            db.add(
                GA4Metric(
                    website_id=website.id,
                    page_id=page.id,
                    date=d,
                    users=users,
                    sessions=sessions,
                    engagement_rate=round(random.uniform(0.62, 0.88), 3),
                    conversions=conversions,
                    revenue=revenue,
                )
            )

        db.add(
            SemrushMetric(
                website_id=website.id,
                page_id=page.id,
                date=today,
                organic_keywords=random.randint(8, 180),
                organic_traffic=base_traffic * 35,
                striking_distance_keywords=random.randint(3, 22),
                opportunity_volume=random.randint(1200, 25000),
                backlinks=random.randint(2, 120),
            )
        )

    db.commit()
    compute_website_priority(db, website.id)
    db.commit()
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
