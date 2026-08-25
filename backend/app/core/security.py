"""Password hashing, JWT issuance/verification and signed state tokens."""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from ..config import settings

TokenType = Literal["access", "refresh"]

# bcrypt truncates at 72 bytes; hashing long passwords silently ignores the tail.
_BCRYPT_MAX_BYTES = 72


class TokenError(Exception):
    """Raised when a token is malformed, expired or of the wrong type."""


# ── Passwords ───────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification of a plaintext password against a stored hash."""
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:_BCRYPT_MAX_BYTES], password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


# ── JWT ─────────────────────────────────────────────────────────────────────


def _create_token(subject: str, token_type: TokenType, expires: timedelta, **claims: Any) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires).timestamp()),
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: int, role: str, email: str) -> str:
    return _create_token(
        str(user_id),
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
        role=role,
        email=email,
    )


def create_refresh_token(user_id: int) -> str:
    return _create_token(
        str(user_id), "refresh", timedelta(days=settings.refresh_token_expire_days)
    )


def decode_token(
    token: str,
    expected_type: TokenType | None = None,
    *,
    require_subject: bool = True,
) -> dict[str, Any]:
    """Decode and validate a JWT, raising :class:`TokenError` on any problem."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("Token is invalid.") from exc

    if expected_type and payload.get("type") != expected_type:
        raise TokenError(f"Expected a {expected_type} token.")
    if require_subject and not payload.get("sub"):
        raise TokenError("Token is missing a subject.")
    return payload


# ── Signed state (OAuth CSRF protection) ────────────────────────────────────


def create_state_token(**claims: Any) -> str:
    """Create a short-lived signed token used as the OAuth ``state`` parameter."""
    now = datetime.now(timezone.utc)
    payload = {
        "type": "oauth_state",
        "iat": int(now.timestamp()),
        "exp": int(
            (now + timedelta(seconds=settings.google_oauth_state_ttl_seconds)).timestamp()
        ),
        "nonce": secrets.token_urlsafe(16),
        **claims,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def verify_state_token(token: str) -> dict[str, Any]:
    """Verify an OAuth ``state`` token and return its claims."""
    payload = decode_token(token, require_subject=False)
    if payload.get("type") != "oauth_state":
        raise TokenError("Not an OAuth state token.")
    return payload


# ── Webhook signatures ──────────────────────────────────────────────────────


def constant_time_compare(a: str, b: str) -> bool:
    """Timing-attack-resistant string comparison."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
