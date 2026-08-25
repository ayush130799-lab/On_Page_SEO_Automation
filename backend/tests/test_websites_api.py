"""Website onboarding, listing and the per-website authorization boundary."""

from __future__ import annotations

from app.models import MemberRole, UserRole, Website, WebsiteMember

from .conftest import auth_headers, make_user

NEW_SITE = {"name": "Acme Marketing", "url": "https://acme.example.com/"}


def test_create_website_normalises_url_and_grants_ownership(client, db, member_user):
    response = client.post("/api/websites", json=NEW_SITE, headers=auth_headers(member_user))
    assert response.status_code == 201
    body = response.json()
    assert body["url"] == "https://acme.example.com/"
    assert body["domain"] == "acme.example.com"

    membership = (
        db.query(WebsiteMember)
        .filter(WebsiteMember.website_id == body["id"], WebsiteMember.user_id == member_user.id)
        .one()
    )
    assert membership.role == MemberRole.OWNER


def test_new_website_reports_every_integration_as_disconnected(client, member_user):
    response = client.post("/api/websites", json=NEW_SITE, headers=auth_headers(member_user))
    integrations = {i["provider"]: i["status"] for i in response.json()["integrations"]}
    assert integrations == {
        "gsc": "not_connected",
        "ga4": "not_connected",
        "semrush": "not_connected",
        "github": "not_connected",
    }


def test_duplicate_website_url_is_rejected(client, member_user):
    headers = auth_headers(member_user)
    client.post("/api/websites", json=NEW_SITE, headers=headers)
    duplicate = client.post("/api/websites", json=NEW_SITE, headers=headers)
    assert duplicate.status_code == 409


def test_github_repo_accepts_url_or_slug(client, member_user):
    headers = auth_headers(member_user)
    from_url = client.post(
        "/api/websites",
        json={**NEW_SITE, "github_repo": "https://github.com/acme/website.git"},
        headers=headers,
    )
    assert from_url.status_code == 201
    assert from_url.json()["github_repo"] == "acme/website"

    from_slug = client.post(
        "/api/websites",
        json={"name": "Two", "url": "https://two.example.com", "github_repo": "acme/two"},
        headers=headers,
    )
    assert from_slug.json()["github_repo"] == "acme/two"


def test_invalid_github_repo_is_rejected(client, member_user):
    response = client.post(
        "/api/websites",
        json={**NEW_SITE, "github_repo": "not a repo slug"},
        headers=auth_headers(member_user),
    )
    assert response.status_code == 422


def test_invalid_render_mode_is_rejected(client, member_user):
    response = client.post(
        "/api/websites",
        json={**NEW_SITE, "render_mode": "sometimes"},
        headers=auth_headers(member_user),
    )
    assert response.status_code == 422


def test_listing_only_returns_websites_the_user_can_see(client, db, member_user, website):
    other = make_user(db, email="other@example.com")
    theirs = Website(name="Other Co", url="https://other.example.com", domain="other.example.com")
    db.add(theirs)
    db.flush()
    db.add(WebsiteMember(website_id=theirs.id, user_id=other.id, role=MemberRole.OWNER))
    db.commit()

    mine = client.get("/api/websites", headers=auth_headers(member_user)).json()
    assert [w["id"] for w in mine["items"]] == [website.id]
    assert mine["total"] == 1


def test_admin_sees_every_website(client, db, admin_user, website):
    listing = client.get("/api/websites", headers=auth_headers(admin_user)).json()
    assert website.id in [w["id"] for w in listing["items"]]


def test_website_search_filter(client, member_user):
    headers = auth_headers(member_user)
    client.post("/api/websites", json={"name": "Alpha", "url": "https://alpha.test"}, headers=headers)
    client.post("/api/websites", json={"name": "Beta", "url": "https://beta.test"}, headers=headers)
    found = client.get("/api/websites?search=Alph", headers=headers).json()
    assert [w["name"] for w in found["items"]] == ["Alpha"]


def test_foreign_website_reads_as_not_found(client, db, website):
    stranger = make_user(db, email="stranger@example.com")
    response = client.get(f"/api/websites/{website.id}", headers=auth_headers(stranger))
    # 404 rather than 403 so the endpoint cannot confirm that the id exists.
    assert response.status_code == 404


def test_viewer_cannot_modify_a_website(client, db, website):
    viewer = make_user(db, email="viewer@example.com", role=UserRole.VIEWER)
    db.add(WebsiteMember(website_id=website.id, user_id=viewer.id, role=MemberRole.VIEWER))
    db.commit()

    headers = auth_headers(viewer)
    assert client.get(f"/api/websites/{website.id}", headers=headers).status_code == 200
    patched = client.patch(
        f"/api/websites/{website.id}", json={"name": "Renamed"}, headers=headers
    )
    assert patched.status_code == 403


def test_owner_can_update_and_delete(client, db, member_user, website):
    headers = auth_headers(member_user)
    patched = client.patch(
        f"/api/websites/{website.id}",
        json={"name": "Renamed Site", "max_pages": 2500, "render_mode": "always"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed Site"
    assert patched.json()["max_pages"] == 2500

    deleted = client.delete(f"/api/websites/{website.id}", headers=headers)
    assert deleted.status_code == 200
    # The delete ran in the request's own session, so query the row rather than reading this
    # session's identity map.
    db.expunge_all()
    assert db.query(Website).filter(Website.id == website.id).first() is None


def test_adding_a_member_grants_access(client, db, member_user, website):
    colleague = make_user(db, email="colleague@example.com")
    assert client.get(
        f"/api/websites/{website.id}", headers=auth_headers(colleague)
    ).status_code == 404

    granted = client.post(
        f"/api/websites/{website.id}/members?email=colleague@example.com&role=editor",
        headers=auth_headers(member_user),
    )
    assert granted.status_code == 200
    assert client.get(
        f"/api/websites/{website.id}", headers=auth_headers(colleague)
    ).status_code == 200


def test_unauthenticated_requests_are_rejected(client, website):
    assert client.get("/api/websites").status_code == 401
    assert client.get(f"/api/websites/{website.id}").status_code == 401
    assert client.post("/api/websites", json=NEW_SITE).status_code == 401
