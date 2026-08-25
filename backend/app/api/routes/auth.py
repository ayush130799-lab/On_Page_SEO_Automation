"""Authentication endpoints: register, login, refresh, current user."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, status

from ...config import settings
from ...core.deps import CurrentUser, DbSession
from ...core.errors import AuthenticationError, ConflictError, ValidationError
from ...core.ratelimit import auth_rate_limit
from ...core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from ...models import User, UserRole, utcnow
from ...schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _issue_tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, user.role, user.email),
        refresh_token=create_refresh_token(user.id),
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(auth_rate_limit)],
)
def register(payload: RegisterRequest, db: DbSession) -> User:
    """Create a user account.

    The first account created becomes the platform administrator; later accounts are members.
    Registration can be closed entirely with ``ALLOW_REGISTRATION=false``.
    """
    if not settings.allow_registration:
        raise ValidationError("Self-registration is disabled on this deployment.")

    email = payload.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise ConflictError("An account with this email already exists.")

    is_first_user = db.query(User.id).first() is None
    user = User(
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=UserRole.ADMIN if is_first_user else UserRole.MEMBER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Registered user %s with role %s", user.id, user.role)
    return user


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(auth_rate_limit)])
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()

    # Same message for "no such user" and "wrong password" so the endpoint cannot be used to
    # enumerate accounts.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthenticationError("Incorrect email or password.")
    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.")

    user.last_login_at = utcnow()
    db.commit()
    return _issue_tokens(user)


@router.post("/refresh", response_model=TokenResponse, dependencies=[Depends(auth_rate_limit)])
def refresh(payload: RefreshRequest, db: DbSession) -> TokenResponse:
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise AuthenticationError("User account is inactive or no longer exists.")
    return _issue_tokens(user)


@router.get("/me", response_model=UserResponse)
def me(user: CurrentUser) -> User:
    return user


@router.get("/config")
def auth_config(_: Request) -> dict[str, bool]:
    """Public capability flags the login screen needs before a user is authenticated."""
    return {"registration_enabled": settings.allow_registration}
