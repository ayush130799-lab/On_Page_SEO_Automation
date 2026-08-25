"""Authentication endpoints and the authorization boundary they establish."""

from __future__ import annotations

from app.models import User, UserRole

from .conftest import auth_headers, make_user

STRONG_PASSWORD = "Str0ng-passphrase!"


def test_first_registered_user_becomes_admin(client):
    response = client.post(
        "/api/auth/register",
        json={"email": "First@Example.com", "password": STRONG_PASSWORD, "full_name": "First"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["role"] == UserRole.ADMIN
    assert body["email"] == "first@example.com"  # normalised
    assert "password" not in body and "password_hash" not in body


def test_second_user_is_a_member(client):
    client.post("/api/auth/register", json={"email": "a@example.com", "password": STRONG_PASSWORD})
    second = client.post(
        "/api/auth/register", json={"email": "b@example.com", "password": STRONG_PASSWORD}
    )
    assert second.status_code == 201
    assert second.json()["role"] == UserRole.MEMBER


def test_duplicate_email_is_rejected(client):
    client.post("/api/auth/register", json={"email": "a@example.com", "password": STRONG_PASSWORD})
    duplicate = client.post(
        "/api/auth/register", json={"email": "A@example.com", "password": STRONG_PASSWORD}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


def test_weak_passwords_are_rejected(client):
    for weak in ("short1!", "allletterspassword", "1234567890123"):
        response = client.post(
            "/api/auth/register", json={"email": "x@example.com", "password": weak}
        )
        assert response.status_code == 422, weak


def test_login_returns_working_tokens(client, db):
    make_user(db, email="login@example.com", password=STRONG_PASSWORD)
    response = client.post(
        "/api/auth/login", json={"email": "login@example.com", "password": STRONG_PASSWORD}
    )
    assert response.status_code == 200
    tokens = response.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] > 0

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "login@example.com"


def test_login_failures_do_not_leak_account_existence(client, db):
    make_user(db, email="known@example.com", password=STRONG_PASSWORD)
    wrong_password = client.post(
        "/api/auth/login", json={"email": "known@example.com", "password": "Wrong-passw0rd!"}
    )
    unknown_user = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "Wrong-passw0rd!"}
    )
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json() == unknown_user.json()


def test_deactivated_account_cannot_log_in(client, db):
    user = make_user(db, email="off@example.com", password=STRONG_PASSWORD)
    user.is_active = False
    db.commit()
    response = client.post(
        "/api/auth/login", json={"email": "off@example.com", "password": STRONG_PASSWORD}
    )
    assert response.status_code == 401


def test_login_records_last_login(client, db):
    user = make_user(db, email="stamp@example.com", password=STRONG_PASSWORD)
    assert user.last_login_at is None
    client.post(
        "/api/auth/login", json={"email": "stamp@example.com", "password": STRONG_PASSWORD}
    )
    db.expire_all()
    assert db.get(User, user.id).last_login_at is not None


def test_refresh_issues_a_new_access_token(client, db):
    make_user(db, email="r@example.com", password=STRONG_PASSWORD)
    tokens = client.post(
        "/api/auth/login", json={"email": "r@example.com", "password": STRONG_PASSWORD}
    ).json()
    refreshed = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


def test_access_token_cannot_be_used_to_refresh(client, db):
    make_user(db, email="r2@example.com", password=STRONG_PASSWORD)
    tokens = client.post(
        "/api/auth/login", json={"email": "r2@example.com", "password": STRONG_PASSWORD}
    ).json()
    response = client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert response.status_code == 401


def test_protected_endpoints_require_a_token(client):
    for headers in ({}, {"Authorization": "Bearer not-a-real-token"}, {"Authorization": "Basic x"}):
        response = client.get("/api/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"


def test_token_for_deleted_user_is_rejected(client, db):
    user = make_user(db, email="ghost@example.com")
    headers = auth_headers(user)
    db.delete(user)
    db.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_registration_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.auth.settings.allow_registration", False)
    response = client.post(
        "/api/auth/register", json={"email": "no@example.com", "password": STRONG_PASSWORD}
    )
    assert response.status_code == 422
    assert client.get("/api/auth/config").json()["registration_enabled"] is False


def test_health_endpoint_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
