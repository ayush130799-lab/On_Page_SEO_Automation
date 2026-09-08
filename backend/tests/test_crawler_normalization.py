"""Tests for URL normalization, path loop detection, asset filtering, and absolute link resolution."""

import pytest
from app.utils.url_utils import (
    absolute_url,
    has_recursive_path_loop,
    is_probably_page,
    normalize_url,
)


def test_normalize_url_strips_tracking_and_redirect_params():
    url = "https://example.com/page?utm_source=google&fbclid=123&redirect=%2Flogin&foo=bar#section"
    normalized = normalize_url(url)
    assert normalized == "https://example.com/page?foo=bar"


def test_normalize_url_trailing_slash_handling():
    assert normalize_url("https://example.com/blog/") == "https://example.com/blog"
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_has_recursive_path_loop():
    assert has_recursive_path_loop("https://example.com/blog/blog/post") is True
    assert has_recursive_path_loop("https://example.com/services/web/services/web") is True
    assert has_recursive_path_loop("https://example.com/path/www.example.com/page") is True
    assert has_recursive_path_loop("https://example.com/industry/education/blog") is False


def test_is_probably_page_filters_assets_and_auth():
    assert is_probably_page("https://example.com/image.png") is False
    assert is_probably_page("https://example.com/style.css") is False
    assert is_probably_page("https://example.com/script.js") is False
    assert is_probably_page("https://example.com/document.pdf") is False
    assert is_probably_page("https://example.com/login") is False
    assert is_probably_page("https://example.com/api/v1/users") is False
    assert is_probably_page("https://example.com/blog/blog/post") is False
    assert is_probably_page("https://example.com/about-us") is True


def test_absolute_url_resolves_malformed_domain_hrefs():
    base = "https://www.webisdom.com/services/digital-marketing"
    href = "www.webisdom.com/services/blog"
    resolved = absolute_url(base, href)
    assert resolved == "https://www.webisdom.com/services/blog"


def test_cleanup_website_parameter_pages_handles_hash_collision(db):
    from app.models import Website, Page
    from app.services.pipeline import cleanup_website_parameter_pages
    from app.utils.url_utils import url_hash

    site = Website(domain="example.com", url="https://example.com", name="Example")
    db.add(site)
    db.flush()

    import hashlib

    canonical_url = "https://example.com/about"
    canonical_hash = url_hash(canonical_url)

    # Pre-existing canonical page (can be inactive or active)
    p1 = Page(
        website_id=site.id,
        url=canonical_url,
        url_hash=canonical_hash,
        path="/about",
        is_active=False,
    )
    # Duplicate parameter variant with a legacy hash from an older crawler version
    param_url = "https://example.com/about?utm_source=google"
    legacy_param_hash = hashlib.sha256(param_url.encode("utf-8")).hexdigest()
    p2 = Page(
        website_id=site.id,
        url=param_url,
        url_hash=legacy_param_hash,
        path="/about",
        is_active=True,
    )
    db.add_all([p1, p2])
    db.commit()

    # Calling cleanup should safely deactivate p2 without crashing with a unique constraint collision
    deactivated = cleanup_website_parameter_pages(db, site)
    assert deactivated == 1

    db.refresh(p2)
    assert p2.is_active is False

