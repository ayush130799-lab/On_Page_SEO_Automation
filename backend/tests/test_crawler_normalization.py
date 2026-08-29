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
