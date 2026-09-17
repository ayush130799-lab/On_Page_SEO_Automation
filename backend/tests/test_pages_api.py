"""GET /api/websites/{id}/pages — the priority table, including the ``rule_id`` filter that
powers the "Most common issues" → affected-pages drilldown.
"""

from __future__ import annotations

import pytest

from app.models import MemberRole, Page, SEOIssue, Severity, Website, WebsiteMember
from app.utils.url_utils import url_hash


@pytest.fixture
def site(db, member_user):
    website = Website(
        name="Acme", url="https://acme.test", domain="acme.test",
        created_by_id=member_user.id,
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    return website


def add_page(db, website, path):
    url = f"https://acme.test{path}"
    page = Page(
        website_id=website.id, url=url, url_hash=url_hash(url), path=path,
        is_active=True, title=f"Page {path}",
    )
    db.add(page)
    db.flush()
    return page


def add_issue(db, page, rule_id, *, severity=Severity.HIGH, is_resolved=False):
    # No parent audit row needed for this filter's purposes — same convention as
    # test_impact_engine_db.py's add_issue.
    issue = SEOIssue(
        seo_audit_id=0, page_id=page.id, rule_id=rule_id, check_type=rule_id,
        severity=severity, title=rule_id.replace("_", " ").title(),
        description=f"{rule_id} is wrong", is_resolved=is_resolved,
    )
    db.add(issue)
    db.flush()
    return issue


def test_rule_id_filter_returns_only_affected_pages(db, client, member_headers, site):
    page_a = add_page(db, site, "/a")
    page_b = add_page(db, site, "/b")
    page_c = add_page(db, site, "/c")
    add_issue(db, page_a, "missing_meta_description")
    add_issue(db, page_b, "missing_meta_description")
    add_issue(db, page_c, "h1_missing")
    db.commit()

    response = client.get(
        f"/api/websites/{site.id}/pages",
        params={"rule_id": "missing_meta_description"},
        headers=member_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    returned_ids = {item["id"] for item in body["items"]}
    assert returned_ids == {page_a.id, page_b.id}


def test_rule_id_filter_excludes_resolved_issues(db, client, member_headers, site):
    page_a = add_page(db, site, "/a")
    add_issue(db, page_a, "missing_meta_description", is_resolved=True)
    db.commit()

    response = client.get(
        f"/api/websites/{site.id}/pages",
        params={"rule_id": "missing_meta_description"},
        headers=member_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_rule_id_filter_unknown_rule_returns_empty_not_error(db, client, member_headers, site):
    add_page(db, site, "/a")
    db.commit()

    response = client.get(
        f"/api/websites/{site.id}/pages",
        params={"rule_id": "does_not_exist"},
        headers=member_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_rule_id_filter_combines_with_existing_filters(db, client, member_headers, site):
    """rule_id must compose with the table's other filters, not replace them."""
    page_a = add_page(db, site, "/a")
    page_b = add_page(db, site, "/b")
    page_a.highest_severity = Severity.CRITICAL
    page_b.highest_severity = Severity.LOW
    add_issue(db, page_a, "missing_meta_description")
    add_issue(db, page_b, "missing_meta_description")
    db.commit()

    response = client.get(
        f"/api/websites/{site.id}/pages",
        params={"rule_id": "missing_meta_description", "severity": "CRITICAL"},
        headers=member_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == page_a.id


def test_rule_id_count_matches_dashboard_top_issues_page_count(db, client, member_headers, site):
    """The count shown by the "Most common issues" card must equal what the drilldown returns."""
    page_a = add_page(db, site, "/a")
    page_b = add_page(db, site, "/b")
    page_c = add_page(db, site, "/c")
    add_issue(db, page_a, "missing_meta_description")
    add_issue(db, page_b, "missing_meta_description")
    add_issue(db, page_c, "missing_meta_description", is_resolved=True)  # must not be counted
    db.commit()

    overview = client.get(f"/api/dashboard/websites/{site.id}", headers=member_headers)
    assert overview.status_code == 200
    top_issues = {i["rule_id"]: i["page_count"] for i in overview.json()["top_issues"]}
    assert top_issues["missing_meta_description"] == 2

    drilldown = client.get(
        f"/api/websites/{site.id}/pages",
        params={"rule_id": "missing_meta_description"},
        headers=member_headers,
    )
    assert drilldown.json()["total"] == top_issues["missing_meta_description"]
