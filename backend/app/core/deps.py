"""FastAPI dependencies: authentication, role checks and per-website authorization."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MemberRole, User, UserRole, Website, WebsiteMember
from .errors import AuthenticationError, AuthorizationError, NotFoundError
from .security import TokenError, decode_token

# ``auto_error=False`` so a missing header produces our typed 401 envelope rather than
# Starlette's default body.
bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]

#: Membership roles that permit mutating a website (triggering crawls, editing integrations).
WRITE_ROLES = {MemberRole.OWNER, MemberRole.EDITOR}


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    """Resolve the authenticated user from a bearer access token."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication credentials were not provided.")

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthenticationError("User account is inactive or no longer exists.")

    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    """Restrict an endpoint to platform administrators."""
    if user.role != UserRole.ADMIN:
        raise AuthorizationError("This action requires an administrator account.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def _membership(db: Session, website_id: int, user_id: int) -> WebsiteMember | None:
    return (
        db.query(WebsiteMember)
        .filter(WebsiteMember.website_id == website_id, WebsiteMember.user_id == user_id)
        .first()
    )


def get_website_for_read(website_id: int, user: CurrentUser, db: DbSession) -> Website:
    """Load a website the caller is allowed to view."""
    website = db.get(Website, website_id)
    if website is None:
        raise NotFoundError(f"Website {website_id} was not found.")
    if user.role == UserRole.ADMIN:
        return website
    if _membership(db, website_id, user.id) is None:
        # Deliberately 404 rather than 403: do not confirm that an id exists to a user with no
        # access to it.
        raise NotFoundError(f"Website {website_id} was not found.")
    return website


def get_website_for_write(website_id: int, user: CurrentUser, db: DbSession) -> Website:
    """Load a website the caller is allowed to modify."""
    website = get_website_for_read(website_id, user, db)
    if user.role == UserRole.ADMIN:
        return website
    if user.role == UserRole.VIEWER:
        raise AuthorizationError("Viewer accounts cannot modify websites.")
    member = _membership(db, website_id, user.id)
    if member is None or member.role not in WRITE_ROLES:
        raise AuthorizationError("You do not have write access to this website.")
    return website


ReadableWebsite = Annotated[Website, Depends(get_website_for_read)]
WritableWebsite = Annotated[Website, Depends(get_website_for_write)]


def accessible_website_ids(db: Session, user: User) -> list[int] | None:
    """Website ids the user may read, or ``None`` meaning "all" (administrators)."""
    if user.role == UserRole.ADMIN:
        return None
    rows = db.query(WebsiteMember.website_id).filter(WebsiteMember.user_id == user.id).all()
    return [row[0] for row in rows]
