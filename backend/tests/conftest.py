"""Shared test fixtures.

The environment is configured *before* any application module is imported so that the cached
``Settings`` instance points at an isolated in-memory database and never at a developer's real
PostgreSQL or third-party credentials.
"""

from __future__ import annotations

import os

os.environ.update(
    {
        "DATABASE_URL": "sqlite://",
        "ENVIRONMENT": "test",
        "SECRET_KEY": "test-secret-key-for-unit-tests-only-0123456789",
        "USE_CELERY": "false",
        "AI_ENABLED": "false",
        "GROQ_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "SEMRUSH_API_KEY": "",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "GITHUB_WEBHOOK_SECRET": "",
        "RATE_LIMIT_ENABLED": "false",
        "ALLOW_LOCAL_CRAWL": "true",
        "RENDER_ENABLED": "false",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.deps import get_current_user  # noqa: E402
from app.core.ratelimit import reset_rate_limits  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import MemberRole, User, UserRole, Website, WebsiteMember  # noqa: E402


@pytest.fixture
def engine():
    """A fresh in-memory SQLite database, shared across connections within one test."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(session_factory):
    """A TestClient wired to the isolated database, with rate limiting reset."""
    reset_rate_limits()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with TestClient(fastapi_app) as test_client:
        yield test_client
    fastapi_app.dependency_overrides.clear()


# ── User / auth helpers ─────────────────────────────────────────────────────


def make_user(db, email="user@example.com", role=UserRole.MEMBER, password="Test-passw0rd!"):
    from app.core.security import hash_password

    user = User(
        email=email, full_name="Test User", password_hash=hash_password(password), role=role
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def auth_headers(user) -> dict[str, str]:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user.id, user.role, user.email)}"}


@pytest.fixture
def admin_user(db):
    return make_user(db, email="admin@example.com", role=UserRole.ADMIN)


@pytest.fixture
def member_user(db):
    return make_user(db, email="member@example.com", role=UserRole.MEMBER)


@pytest.fixture
def admin_headers(admin_user):
    return auth_headers(admin_user)


@pytest.fixture
def member_headers(member_user):
    return auth_headers(member_user)


@pytest.fixture
def as_admin(admin_user):
    """Bypass the bearer dependency entirely (handy for non-auth-focused tests)."""
    fastapi_app.dependency_overrides[get_current_user] = lambda: admin_user
    yield admin_user
    fastapi_app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def website(db, member_user):
    site = Website(
        name="Example Site",
        url="https://example.com",
        domain="example.com",
        created_by_id=member_user.id,
    )
    db.add(site)
    db.flush()
    db.add(WebsiteMember(website_id=site.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(site)
    return site
