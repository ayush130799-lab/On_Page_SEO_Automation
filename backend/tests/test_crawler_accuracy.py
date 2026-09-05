"""Crawler accuracy regression tests.

These tests verify that the extractor, URL utilities, sitemap parser, SEO rules,
and robots parser produce correct, JetOctopus-comparable results.

Each test class maps to a specific bug or accuracy requirement from the fix plan.
"""

from __future__ import annotations

import pytest

from app.models.enums import Severity
from app.services.crawler.extractor import ExtractedPage, empty_page, extract_page
from app.services.crawler.robots import parse_robots
from app.services.crawler.sitemap import collect_sitemap_urls, parse_sitemap_document
from app.services.seo.rules.indexability import (
    check_canonical,
    check_http_status,
    check_multiple_canonical,
    check_redirect_chain,
    check_robots,
)
from app.services.seo.rules.media_links import check_image_alt
from app.services.seo.rules.structure import check_content, check_h1
from app.services.seo.rules.metadata import check_meta_description, check_title
from app.utils.url_utils import (
    has_recursive_path_loop,
    is_probably_page,
    is_same_domain,
    normalize_url,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _page(**kwargs) -> ExtractedPage:
    """Build a minimal ExtractedPage for rule testing."""
    meta_robots = kwargs.pop("robots_directive", None)
    defaults = {
        "url": "https://example.com/",
        "status_code": 200,
        "title": "Test Page Title That Is Good Length",
        "meta_description": "A useful meta description that is the right length for SEO purposes here.",
        "meta_robots": meta_robots,
        "h1": "Main Heading",
        "h1_count": 1,
        "word_count": 500,
        "content": "A " * 250,
        "canonical_url": "https://example.com/",
        "canonical_count": 1,
        "canonical_raw": "/",
        "image_count": 0,
        "missing_alt_count": 0,
        "empty_alt_count": 0,
        "internal_links": ["https://example.com/a", "https://example.com/b"],
    }
    defaults.update(kwargs)
    return ExtractedPage(**defaults)


def _extract(html: str, url: str = "https://example.com/", base_domain: str = "example.com") -> ExtractedPage:
    return extract_page(url, html, base_domain, 200)


# ── 1. Missing ALT vs empty ALT ──────────────────────────────────────────────

class TestImageAltDetection:
    def test_truly_missing_alt_counted(self):
        """Images with no alt attribute at all are missing_alt_count."""
        html = '<html><body><img src="photo.jpg"><img src="header-bg.png"></body></html>'
        page = _extract(html)
        assert page.missing_alt_count == 2
        assert page.empty_alt_count == 0
        assert page.image_count == 2

    def test_empty_alt_is_decorative(self):
        """Images with alt=\"\" are intentionally decorative — empty_alt_count, NOT missing_alt_count."""
        html = '<html><body><img src="deco.png" alt=""></body></html>'
        page = _extract(html)
        assert page.missing_alt_count == 0
        assert page.empty_alt_count == 1

    def test_text_alt_is_neither_missing_nor_empty(self):
        """Images with a text alt are fully OK."""
        html = '<html><body><img src="dog.jpg" alt="A dog sitting in the park"></body></html>'
        page = _extract(html)
        assert page.missing_alt_count == 0
        assert page.empty_alt_count == 0

    def test_mixed_alt_states(self):
        """Mixed: 1 missing, 1 empty, 1 with text."""
        html = (
            '<html><body>'
            '<img src="a.jpg">'         # missing
            '<img src="b.jpg" alt="">'  # decorative
            '<img src="c.jpg" alt="Cat">'  # ok
            '</body></html>'
        )
        page = _extract(html)
        assert page.image_count == 3
        assert page.missing_alt_count == 1
        assert page.empty_alt_count == 1

    def test_tracking_pixel_excluded_from_counts(self):
        """1x1 tracking pixels are not counted as real images."""
        html = (
            '<html><body>'
            '<img src="photo.jpg" alt="Photo">'
            '<img src="pixel.gif" width="1" height="1">'  # tracking pixel
            '</body></html>'
        )
        page = _extract(html)
        # Only the real image should be counted
        assert page.image_count == 1
        assert page.missing_alt_count == 0

    def test_image_alt_rule_uses_missing_count(self):
        """Rule only fires on truly missing alt (not empty alt)."""
        page = _page(image_count=3, missing_alt_count=0, empty_alt_count=2)
        result = check_image_alt(page)
        assert result.status == "pass"
        assert "decorative" in result.details.lower() or "2" in result.details

    def test_image_alt_rule_fails_on_missing(self):
        page = _page(image_count=4, missing_alt_count=3, empty_alt_count=1)
        result = check_image_alt(page)
        assert result.status == "fail"
        assert result.evidence["missing_alt"] == 3
        assert result.evidence.get("empty_alt_decorative") == 1


# ── 2. Word count methodology ─────────────────────────────────────────────────

class TestWordCountMethodology:
    def test_nav_excluded_from_word_count(self):
        """Navigation text must NOT be included in word count."""
        html = (
            '<html><body>'
            '<nav>Home About Contact Blog</nav>'
            '<main><p>' + ('content word ' * 50) + '</p></main>'
            '</body></html>'
        )
        page = _extract(html)
        # The nav adds 4 words; body has 100 words (50 * "content word")
        assert page.word_count == 100

    def test_footer_excluded_from_word_count(self):
        html = (
            '<html><body>'
            '<article><p>' + ('article word ' * 30) + '</p></article>'
            '<footer>Privacy Policy Terms Copyright 2024</footer>'
            '</body></html>'
        )
        page = _extract(html)
        # Footer has 5 words; article has 60 words (30 * "article word")
        assert page.word_count == 60

    def test_header_excluded_from_word_count(self):
        html = (
            '<html><body>'
            '<header>Logo Search Hamburger Menu</header>'
            '<article><p>' + ('body word ' * 20) + '</p></article>'
            '</body></html>'
        )
        page = _extract(html)
        # Header has 4 words; article has 40 words
        assert page.word_count == 40

    def test_aside_excluded_from_word_count(self):
        html = (
            '<html><body>'
            '<main><p>' + ('main word ' * 25) + '</p></main>'
            '<aside>Related sidebar content extra filler words here</aside>'
            '</body></html>'
        )
        page = _extract(html)
        # Aside has 7 words; main has 50 words
        assert page.word_count == 50

    def test_word_count_computed_before_truncation(self):
        """Word count reflects the full page, not the 20000-char stored slice."""
        long_content = "word " * 5000  # 25000 chars, 5000 words
        html = f'<html><body><article><p>{long_content}</p></article></body></html>'
        page = _extract(html)
        assert page.word_count >= 5000


# ── 3. Canonical detection ─────────────────────────────────────────────────────

class TestCanonicalExtraction:
    def test_canonical_detected(self):
        html = '<html><head><link rel="canonical" href="https://example.com/about"></head><body>x</body></html>'
        page = _extract(html, url="https://example.com/about/")
        assert page.canonical_url == "https://example.com/about"
        assert page.canonical_count == 1

    def test_canonical_raw_stored(self):
        """Raw href before resolution must be stored."""
        html = '<html><head><link rel="canonical" href="/about/"></head><body>x</body></html>'
        page = _extract(html, url="https://example.com/about/")
        assert page.canonical_raw == "/about/"

    def test_canonical_missing(self):
        html = '<html><head><title>No canonical</title></head><body>x</body></html>'
        page = _extract(html)
        assert page.canonical_url is None
        assert page.canonical_count == 0

    def test_multiple_canonical_detected(self):
        html = (
            '<html><head>'
            '<link rel="canonical" href="https://example.com/a">'
            '<link rel="canonical" href="https://example.com/b">'
            '</head><body>x</body></html>'
        )
        page = _extract(html)
        assert page.canonical_count == 2

    def test_multiple_canonical_rule_fires(self):
        page = _page(canonical_count=2, canonical_url="https://example.com/a")
        result = check_multiple_canonical(page)
        assert result.status == "fail"
        assert result.severity == Severity.CRITICAL
        assert result.evidence["canonical_count"] == 2

    def test_single_canonical_rule_passes(self):
        page = _page(canonical_count=1)
        result = check_multiple_canonical(page)
        assert result.status == "pass"

    def test_no_canonical_rule_warns(self):
        page = _page(canonical_url=None, canonical_count=0)
        result = check_canonical(page)
        assert result.status == "warning"
        assert result.severity == Severity.HIGH

    def test_trailing_slash_canonical_resolves(self):
        """Canonical href=/about/ on page /about/ should resolve to https://example.com/about."""
        html = '<html><head><link rel="canonical" href="/about/"></head><body>x</body></html>'
        page = _extract(html, url="https://example.com/about/")
        assert page.canonical_url is not None
        assert page.canonical_url.startswith("https://")


# ── 4. Robots directive handling ──────────────────────────────────────────────

class TestRobotsRules:
    def test_noindex_detected(self):
        page = _page(robots_directive="noindex, follow")
        result = check_robots(page)
        assert result.status == "fail"
        assert result.severity == Severity.CRITICAL

    def test_x_robots_noindex_detected(self):
        page = _page(robots_directive=None, x_robots_tag="noindex")
        result = check_robots(page)
        assert result.status == "fail"
        assert result.severity == Severity.CRITICAL

    def test_x_robots_none_is_noindex_nofollow(self):
        """x-robots-tag: none = noindex,nofollow and should be CRITICAL."""
        page = _page(robots_directive=None, x_robots_tag="none")
        result = check_robots(page)
        assert result.status == "fail"
        assert result.severity == Severity.CRITICAL

    def test_empty_robots_is_ok(self):
        page = _page(robots_directive=None, x_robots_tag=None)
        result = check_robots(page)
        assert result.status == "pass"

    def test_nofollow_only_is_high_not_critical(self):
        page = _page(robots_directive="follow, nofollow", x_robots_tag=None)
        result = check_robots(page)
        assert result.status == "warning"
        assert result.severity == Severity.HIGH

    def test_index_follow_is_ok(self):
        page = _page(robots_directive="index, follow")
        result = check_robots(page)
        assert result.status == "pass"

    def test_null_safety_no_spurious_none_match(self):
        """An empty combined string must not match 'none'."""
        page = _page(robots_directive="", x_robots_tag="")
        result = check_robots(page)
        assert result.status == "pass"


# ── 5. URL admission filter ───────────────────────────────────────────────────

class TestUrlAdmissionFilter:
    def test_api_segment_blocks_exact_api(self):
        assert is_probably_page("https://example.com/wp-admin/edit.php") is False
        assert is_probably_page("https://example.com/wp-json/v2/posts") is False

    def test_api_docs_not_blocked(self):
        """Paths like /api-docs/ should NOT be blocked."""
        assert is_probably_page("https://example.com/api-docs/") is True

    def test_therapy_not_blocked(self):
        """Word 'therapy' contains 'api' but must not be blocked."""
        assert is_probably_page("https://example.com/therapy-sessions/") is True

    def test_sign_in_not_blocked(self):
        """Path /sign-in/ must not trigger the ccTLD loop detector."""
        assert is_probably_page("https://example.com/sign-in/") is True

    def test_co_founder_not_blocked(self):
        """Path /co-founder/ must not trigger the ccTLD detector."""
        assert is_probably_page("https://example.com/co-founder/") is True

    def test_login_page_is_blocked(self):
        assert is_probably_page("https://example.com/login") is False

    def test_feed_is_blocked(self):
        assert is_probably_page("https://example.com/feed") is False

    def test_asset_extensions_blocked(self):
        assert is_probably_page("https://example.com/style.css") is False
        assert is_probably_page("https://example.com/app.js") is False
        assert is_probably_page("https://example.com/photo.jpg") is False

    def test_blog_article_allowed(self):
        assert is_probably_page("https://example.com/blog/how-to-use-api-effectively") is True


# ── 6. Recursive path loop detection ─────────────────────────────────────────

class TestPathLoopDetection:
    def test_actual_loop_detected(self):
        assert has_recursive_path_loop("https://example.com/blog/blog/post") is True

    def test_adjacent_duplicate_detected(self):
        assert has_recursive_path_loop("https://example.com/shoes/shoes/post") is True

    def test_sign_in_not_a_loop(self):
        assert has_recursive_path_loop("https://example.com/sign-in") is False

    def test_co_founder_not_a_loop(self):
        assert has_recursive_path_loop("https://example.com/co-founder") is False

    def test_opt_in_not_a_loop(self):
        assert has_recursive_path_loop("https://example.com/opt-in") is False

    def test_normal_path_not_a_loop(self):
        assert has_recursive_path_loop("https://example.com/about/team") is False

    def test_embedded_hostname_detected(self):
        assert has_recursive_path_loop("https://example.com/redirect/www.spam.com") is True


# ── 7. Pagination link discovery ──────────────────────────────────────────────

class TestPaginationDiscovery:
    def test_next_page_detected(self):
        html = (
            '<html><head>'
            '<link rel="next" href="https://example.com/blog?page=2">'
            '</head><body><p>Blog content</p></body></html>'
        )
        page = _extract(html, url="https://example.com/blog")
        assert page.pagination_next == "https://example.com/blog?page=2"

    def test_prev_page_detected(self):
        html = (
            '<html><head>'
            '<link rel="prev" href="https://example.com/blog">'
            '<link rel="next" href="https://example.com/blog?page=3">'
            '</head><body>p</body></html>'
        )
        page = _extract(html, url="https://example.com/blog?page=2")
        assert page.pagination_prev == "https://example.com/blog"
        assert page.pagination_next == "https://example.com/blog?page=3"

    def test_pagination_added_to_internal_links(self):
        """Next page URL must be included in internal_links for crawl discovery."""
        html = (
            '<html><head>'
            '<link rel="next" href="/blog?page=2">'
            '</head><body><a href="/about">About</a></body></html>'
        )
        page = _extract(html, url="https://example.com/blog")
        assert "https://example.com/blog?page=2" in page.internal_links

    def test_no_pagination_when_absent(self):
        html = '<html><head><title>Blog</title></head><body><p>content</p></body></html>'
        page = _extract(html)
        assert page.pagination_next is None
        assert page.pagination_prev is None


# ── 8. hreflang — RSS exclusion ──────────────────────────────────────────────

class TestHreflangExtraction:
    def test_hreflang_detected(self):
        html = (
            '<html><head>'
            '<link rel="alternate" hreflang="en" href="https://example.com/en/">'
            '<link rel="alternate" hreflang="fr" href="https://example.com/fr/">'
            '</head><body>x</body></html>'
        )
        page = _extract(html)
        assert len(page.hreflang) == 2
        langs = {h["lang"] for h in page.hreflang}
        assert "en" in langs and "fr" in langs

    def test_rss_alternate_excluded(self):
        """RSS/Atom alternates must NOT appear in hreflang."""
        html = (
            '<html><head>'
            '<link rel="alternate" type="application/rss+xml" href="/feed.rss">'
            '<link rel="alternate" hreflang="en" href="https://example.com/en/">'
            '</head><body>x</body></html>'
        )
        page = _extract(html)
        assert len(page.hreflang) == 1
        assert page.hreflang[0]["lang"] == "en"

    def test_atom_alternate_excluded(self):
        html = (
            '<html><head>'
            '<link rel="alternate" type="application/atom+xml" href="/feed.atom">'
            '<link rel="alternate" hreflang="de" href="https://example.com/de/">'
            '</head><body>x</body></html>'
        )
        page = _extract(html)
        assert len(page.hreflang) == 1


# ── 9. Link classification — sponsored / ugc ─────────────────────────────────

class TestLinkClassification:
    def test_sponsored_link_counted(self):
        html = (
            '<html><body>'
            '<a href="https://partner.com/" rel="sponsored">Partner</a>'
            '</body></html>'
        )
        page = _extract(html, url="https://example.com/", base_domain="example.com")
        assert page.sponsored_link_count == 1

    def test_ugc_link_counted(self):
        html = (
            '<html><body>'
            '<a href="https://other.com/post" rel="ugc">User post</a>'
            '</body></html>'
        )
        page = _extract(html, url="https://example.com/", base_domain="example.com")
        assert page.ugc_link_count == 1

    def test_nofollow_counted_separately(self):
        html = (
            '<html><body>'
            '<a href="https://other.com/" rel="nofollow">Nofollow</a>'
            '<a href="https://partner.com/" rel="sponsored nofollow">Sponsored</a>'
            '</body></html>'
        )
        page = _extract(html, url="https://example.com/", base_domain="example.com")
        assert page.nofollow_link_count == 2
        assert page.sponsored_link_count == 1


# ── 10. Open Graph detection accuracy ────────────────────────────────────────

class TestOpenGraphDetection:
    def test_og_detected_with_title(self):
        """og:title marks OG as present."""
        html = (
            '<html><head>'
            '<meta property="og:title" content="My Page">'
            '</head><body>x</body></html>'
        )
        page = _extract(html)
        assert page.has_open_graph is True

    def test_og_title_plus_description_sufficient(self):
        html = (
            '<html><head>'
            '<meta property="og:title" content="My Page">'
            '<meta property="og:description" content="Description">'
            '</head><body>x</body></html>'
        )
        page = _extract(html)
        assert page.has_open_graph is True

    def test_og_title_plus_image_sufficient(self):
        html = (
            '<html><head>'
            '<meta property="og:title" content="My Page">'
            '<meta property="og:image" content="https://example.com/og.jpg">'
            '</head><body>x</body></html>'
        )
        page = _extract(html)
        assert page.has_open_graph is True

    def test_no_og_tags_at_all(self):
        html = '<html><head><title>Plain Page</title></head><body>x</body></html>'
        page = _extract(html)
        assert page.has_open_graph is False


# ── 11. Redirect chain counting ───────────────────────────────────────────────

class TestRedirectChainRule:
    def test_no_redirects_passes(self):
        page = _page(redirect_chain=[], final_url="https://example.com/")
        result = check_redirect_chain(page)
        assert result.status == "pass"

    def test_one_hop_warns_low(self):
        page = _page(
            redirect_chain=["https://example.com/old/"],
            final_url="https://example.com/new/",
        )
        result = check_redirect_chain(page)
        assert result.status == "warning"
        assert result.severity == Severity.LOW

    def test_two_hops_warns_medium(self):
        page = _page(
            redirect_chain=["https://example.com/a", "https://example.com/b"],
            final_url="https://example.com/c",
        )
        result = check_redirect_chain(page)
        assert result.status == "warning"
        assert result.severity == Severity.MEDIUM

    def test_three_plus_hops_fails_high(self):
        page = _page(
            redirect_chain=["https://example.com/a", "https://example.com/b", "https://example.com/c"],
            final_url="https://example.com/d",
        )
        result = check_redirect_chain(page)
        assert result.status == "fail"
        assert result.severity == Severity.HIGH

    def test_trailing_slash_redirect_is_ok(self):
        """A single hop that is just the normalised form of the same URL is not a real redirect."""
        page = _page(
            redirect_chain=["https://example.com/about"],
            final_url="https://example.com/about",
        )
        result = check_redirect_chain(page)
        assert result.status == "pass"


# ── 12. Sitemap parser ────────────────────────────────────────────────────────

class TestSitemapParser:
    def test_simple_urlset(self):
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/</loc><changefreq>daily</changefreq></url>
          <url><loc>https://example.com/about</loc></url>
          <url><loc>https://example.com/contact</loc></url>
        </urlset>"""
        entries, _, used_fallback = parse_sitemap_document(xml)
        assert not used_fallback, "well-formed XML must not need the regex fallback"
        assert [e.loc for e in entries] == [
            "https://example.com/",
            "https://example.com/about",
            "https://example.com/contact",
        ]
        # The optional metadata the specification defines is preserved, not discarded.
        assert entries[0].changefreq == "daily"

    def test_sitemapindex_nested(self):
        xml = """<?xml version="1.0"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.com/sitemap-posts.xml</loc></sitemap>
          <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
        </sitemapindex>"""
        entries, nested, _ = parse_sitemap_document(xml)
        assert nested == [
            "https://example.com/sitemap-posts.xml",
            "https://example.com/sitemap-pages.xml",
        ]
        assert entries == []  # No page URLs in an index

    def test_sitemap_does_not_confuse_changefreq_with_nested_sitemap(self):
        """changefreq / priority / lastmod tags inside <url> must not be mis-parsed as sitemaps."""
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://example.com/page</loc>
            <changefreq>weekly</changefreq>
            <priority>0.8</priority>
            <lastmod>2024-01-01</lastmod>
          </url>
        </urlset>"""
        entries, nested, _ = parse_sitemap_document(xml)
        assert len(entries) == 1
        assert entries[0].loc == "https://example.com/page"
        assert entries[0].changefreq == "weekly"
        assert entries[0].priority == 0.8
        assert entries[0].lastmod == "2024-01-01"
        assert nested == []


# ── 13. robots.txt parsing ───────────────────────────────────────────────────

class TestRobotsParser:
    def test_wildcard_disallow(self):
        txt = "User-agent: *\nDisallow: /private/"
        rules = parse_robots(txt)
        assert rules.fetched is True
        assert not rules.is_allowed("https://example.com/private/page")
        assert rules.is_allowed("https://example.com/public/page")

    def test_allow_overrides_disallow(self):
        txt = "User-agent: *\nDisallow: /\nAllow: /public/"
        rules = parse_robots(txt)
        assert rules.is_allowed("https://example.com/public/page")
        assert not rules.is_allowed("https://example.com/private")

    def test_empty_disallow_means_allow_all(self):
        txt = "User-agent: *\nDisallow:"
        rules = parse_robots(txt)
        assert rules.is_allowed("https://example.com/anything")

    def test_missing_robots_txt_allows_everything(self):
        from app.services.crawler.robots import RobotsRules
        rules = RobotsRules(fetched=False)
        assert rules.is_allowed("https://example.com/secret")

    def test_sitemap_directives_extracted(self):
        txt = "User-agent: *\nDisallow:\nSitemap: https://example.com/sitemap.xml"
        rules = parse_robots(txt)
        assert "https://example.com/sitemap.xml" in rules.sitemaps


# ── 14. URL normalisation ─────────────────────────────────────────────────────

class TestUrlNormalisation:
    def test_trailing_slash_stripped(self):
        assert normalize_url("https://example.com/about/") == "https://example.com/about"

    def test_fragment_stripped(self):
        assert normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_utm_stripped(self):
        url = normalize_url("https://example.com/p?utm_source=google&id=7")
        assert "utm_source" not in url
        assert "id=7" in url

    def test_default_port_stripped(self):
        assert normalize_url("https://example.com:443/a") == "https://example.com/a"

    def test_scheme_case_normalised(self):
        assert normalize_url("HTTPS://Example.COM/") == "https://example.com/"

    def test_query_params_sorted(self):
        assert normalize_url("https://example.com/p?b=2&a=1") == normalize_url("https://example.com/p?a=1&b=2")

    def test_root_path_preserved(self):
        assert normalize_url("https://example.com") == "https://example.com/"


# ── 15. SEO rule: content length ─────────────────────────────────────────────

class TestContentLengthRule:
    def test_thin_content_fails(self):
        page = _page(content="short", word_count=5)
        result = check_content(page)
        assert result.status == "fail"
        assert result.severity == Severity.HIGH

    def test_adequate_content_passes(self):
        page = _page(content="word " * 200, word_count=200)
        result = check_content(page)
        assert result.status == "pass"


# ── 16. SEO rule: title ───────────────────────────────────────────────────────

class TestTitleRule:
    def test_missing_title_fails(self):
        page = _page(title=None)
        result = check_title(page)
        assert result.status == "fail"
        assert result.severity == Severity.HIGH

    def test_short_title_warns(self):
        page = _page(title="Short")
        result = check_title(page)
        assert result.status == "warning"
        assert result.severity == Severity.MEDIUM

    def test_good_title_passes(self):
        page = _page(title="This is a Perfect Length Title Tag For SEO")
        result = check_title(page)
        assert result.status == "pass"


# ── 17. Empty page quality indicator ─────────────────────────────────────────

class TestCrawlQuality:
    def test_failed_fetch_quality(self):
        page = empty_page("https://example.com/", 0, "Connection refused")
        assert page.crawl_quality == "failed"

    def test_partial_fetch_quality(self):
        page = empty_page("https://example.com/404", 404, "No HTML body (HTTP 404)")
        assert page.crawl_quality == "partial"

    def test_successful_extraction_quality(self):
        html = '<html><body><h1>Hello</h1></body></html>'
        page = _extract(html)
        assert page.crawl_quality == "ok"
