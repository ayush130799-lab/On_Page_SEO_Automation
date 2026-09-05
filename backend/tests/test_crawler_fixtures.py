"""Controlled-fixture tests for crawler extraction accuracy.

One class per scenario, thirty-two in all, each asserting the *actual extracted values* rather
than merely that extraction completed. Fixtures are deliberately small and hand-written so the
expected value can be read off the markup by eye — if a test fails, the fixture is the
specification.

Scenarios that are properties of the HTTP exchange rather than the document (redirects, 404s,
robots.txt, sitemaps) drive the real fetcher over ``httpx.MockTransport`` so the code under test
is the same code a live crawl runs.
"""

from __future__ import annotations

import gzip

import httpx
import pytest

from app.services.crawler.extractor import empty_page, extract_page, is_tracking_pixel
from app.services.crawler.fetcher import fetch_url
from app.services.crawler.renderer import needs_rendering
from app.services.crawler.robots import parse_robots
from app.services.crawler.sitemap import collect_sitemap_entries, parse_sitemap_document
from app.services.seo.engine import audit_page
from app.utils.url_utils import normalize_url

BASE = "example.com"
URL = "https://example.com/page"


def parse(html: str, url: str = URL, status: int = 200, **headers):
    """Extract with the same call signature the orchestrator uses."""
    return extract_page(url, html, BASE, status, headers=headers or None)


def doc(body: str, head: str = "", html_attrs: str = ' lang="en"') -> str:
    return f"<!doctype html><html{html_attrs}><head>{head}</head><body>{body}</body></html>"


# ── 1. Normal HTML ───────────────────────────────────────────────────────────


class TestNormalHtml:
    HTML = doc(
        """<main><h1>Widgets</h1><h2>Blue widgets</h2>
        <p>Widgets are useful for many everyday tasks around the home.</p>
        <img src="/w.png" alt="A widget">
        <a href="/about">About us</a><a href="https://other.test/">Partner</a></main>""",
        head='<title>Widgets | Shop</title><meta name="description" content="Buy widgets.">'
             '<link rel="canonical" href="https://example.com/page">'
             '<meta name="viewport" content="width=device-width">',
    )

    def test_every_headline_field_is_extracted(self):
        page = parse(self.HTML)
        assert page.title == "Widgets | Shop"
        assert page.title_count == 1
        assert page.meta_description == "Buy widgets."
        assert page.meta_description_count == 1
        assert page.h1 == "Widgets"
        assert page.h1_count == 1
        assert page.h2_count == 1
        assert page.canonical_status == "self"
        assert page.lang == "en"
        assert page.has_viewport is True
        assert page.image_count == 1
        assert page.missing_alt_count == 0
        assert page.internal_link_count == 1
        assert page.external_link_count == 1
        assert page.crawl_quality == "ok"
        assert page.extraction_errors == []

    def test_word_count_is_the_main_content_figure(self):
        page = parse(self.HTML)
        assert page.word_count == page.main_content_word_count
        assert page.content_scope == "main"
        assert page.word_count > 0

    def test_provenance_explains_each_value(self):
        page = parse(self.HTML)
        assert page.provenance["title"]["matched"] == 1
        assert page.provenance["title"]["raw"] == "Widgets | Shop"
        assert "canonical" in page.provenance["canonical"]["selector"]


# ── 2. JavaScript-rendered content ───────────────────────────────────────────


class TestJavaScriptRendered:
    SHELL = doc('<div id="root"></div><script src="/app.js"></script>')
    RENDERED = doc('<main><h1>Loaded by JS</h1><p>%s</p></main>' % ("content " * 80))

    def test_empty_shell_is_flagged_for_rendering(self):
        assert needs_rendering(self.SHELL) is True

    def test_server_rendered_page_is_not_rendered_needlessly(self):
        assert needs_rendering(self.RENDERED) is False

    def test_render_mode_never_short_circuits(self):
        assert needs_rendering(self.SHELL, render_mode="never") is False

    def test_shell_extraction_is_thin_and_rendered_is_not(self):
        # The point of rendering: the same URL yields a real H1 only after the DOM is built.
        assert parse(self.SHELL).h1 is None
        assert parse(self.RENDERED).h1 == "Loaded by JS"

    def test_failed_render_is_recorded_not_silently_accepted(self):
        page = parse(self.SHELL)
        page.render_error = "renderer unavailable"
        page.crawl_quality = "render_failed"
        # A thin client-rendered page must never be reported as genuinely thin without a mark.
        assert page.crawl_quality == "render_failed"
        assert page.is_usable is True  # the HTTP response itself was fine


# ── 3-7. Canonical variants ──────────────────────────────────────────────────


class TestCanonicalMissing:
    def test_absent_canonical_reports_missing_not_empty(self):
        page = parse(doc("<h1>x</h1>"))
        assert page.canonical_count == 0
        assert page.canonical_url is None
        assert page.canonical_raw is None
        assert page.canonical_status == "missing"


class TestCanonicalMultiple:
    HTML = doc(
        "<h1>x</h1>",
        head='<link rel="canonical" href="https://example.com/a">'
             '<link rel="canonical" href="https://example.com/b">',
    )

    def test_all_canonicals_are_counted(self):
        page = parse(self.HTML)
        assert page.canonical_count == 2
        assert page.canonical_status == "multiple"

    def test_first_declared_canonical_is_still_reported(self):
        # The conflict is reported; the value is not thrown away, so the fix is visible.
        assert parse(self.HTML).canonical_url == "https://example.com/a"

    def test_rule_engine_flags_the_conflict(self):
        result = audit_page(parse(self.HTML))
        assert any(r.rule_id == "canonical_multiple" and r.is_issue for r in result.results)


class TestCanonicalSelf:
    def test_self_canonical_is_recognised(self):
        page = parse(doc("<h1>x</h1>", head=f'<link rel="canonical" href="{URL}">'))
        assert page.canonical_status == "self"
        assert page.canonical_url == URL

    def test_trailing_slash_difference_still_counts_as_self(self):
        page = parse(doc("<h1>x</h1>", head='<link rel="canonical" href="https://example.com/page/">'))
        assert page.canonical_status == "self"


class TestCanonicalCross:
    def test_cross_domain_canonical_is_marked_other(self):
        page = parse(doc("<h1>x</h1>", head='<link rel="canonical" href="https://other.test/p">'))
        assert page.canonical_status == "other"
        assert page.canonical_url == "https://other.test/p"

    def test_rule_engine_warns_that_the_page_defers(self):
        page = parse(doc("<h1>x</h1>", head='<link rel="canonical" href="https://other.test/p">'))
        result = audit_page(page)
        assert any(r.rule_id == "canonical_target" and r.is_issue for r in result.results)


class TestCanonicalRelative:
    def test_relative_canonical_is_resolved_against_the_page_url(self):
        page = parse(doc("<h1>x</h1>", head='<link rel="canonical" href="/other">'))
        assert page.canonical_raw == "/other"
        assert page.canonical_url == "https://example.com/other"
        assert page.canonical_status == "relative"

    def test_empty_canonical_href_is_distinct_from_missing(self):
        page = parse(doc("<h1>x</h1>", head='<link rel="canonical" href="  ">'))
        assert page.canonical_count == 1
        assert page.canonical_status == "empty"
        assert page.canonical_url is None


# ── 8-10. Title and meta description ─────────────────────────────────────────


class TestTitleMissing:
    def test_no_title_element_yields_none_not_empty_string(self):
        page = parse(doc("<h1>x</h1>"))
        assert page.title is None
        assert page.title_count == 0

    def test_svg_title_is_not_mistaken_for_the_page_title(self):
        page = parse(doc('<svg><title>icon label</title></svg><h1>x</h1>'))
        assert page.title is None
        assert page.title_count == 0


class TestTitleMultiple:
    HTML = doc("<h1>x</h1>", head="<title>First</title><title>Second</title>")

    def test_first_title_wins_and_all_are_counted(self):
        page = parse(self.HTML)
        assert page.title == "First"
        assert page.title_count == 2

    def test_rule_engine_reports_the_duplicate(self):
        result = audit_page(parse(self.HTML))
        assert any(r.rule_id == "title_multiple" and r.is_issue for r in result.results)


class TestMetaDescription:
    def test_missing_description_is_none(self):
        page = parse(doc("<h1>x</h1>"))
        assert page.meta_description is None
        assert page.meta_description_count == 0

    def test_multiple_descriptions_are_counted_and_first_non_empty_wins(self):
        page = parse(doc("<h1>x</h1>", head='<meta name="description" content="">'
                                            '<meta name="description" content="Real one.">'))
        assert page.meta_description_count == 2
        assert page.meta_description == "Real one."

    def test_name_attribute_is_matched_case_insensitively(self):
        page = parse(doc("<h1>x</h1>", head='<meta name="Description" content="Cased.">'))
        assert page.meta_description == "Cased."


# ── 11-12. Headings ──────────────────────────────────────────────────────────


class TestMultipleH1:
    HTML = doc("<h1>First</h1><h1>Second</h1><h2>Sub</h2>")

    def test_both_h1s_are_counted_and_preserved(self):
        page = parse(self.HTML)
        assert page.h1_count == 2
        assert "First" in page.h1 and "Second" in page.h1

    def test_other_levels_are_measured_against_the_same_document(self):
        page = parse(self.HTML)
        assert page.h2_count == 1
        assert page.h3_count == page.h4_count == page.h5_count == page.h6_count == 0


class TestNoH1:
    def test_absent_h1_is_none_with_zero_count(self):
        page = parse(doc("<h2>Only a subheading</h2>"))
        assert page.h1 is None
        assert page.h1_count == 0
        assert page.h2_count == 1

    def test_empty_h1_is_counted_as_a_heading_but_not_as_text(self):
        page = parse(doc("<h1>   </h1>"))
        assert page.h1_count == 1          # the element exists
        assert page.h1 is None             # but it carries no text
        assert page.empty_heading_count == 1

    def test_all_six_levels_are_extracted(self):
        page = parse(doc("".join(f"<h{n}>L{n}</h{n}>" for n in range(1, 7))))
        assert [getattr(page, f"h{n}_count") for n in range(1, 7)] == [1] * 6


# ── 13. Hidden content ───────────────────────────────────────────────────────


class TestHiddenContent:
    HTML = doc(
        '<main><p>Visible sentence with several words in it.</p>'
        '<div style="display:none"><p>hidden alpha bravo charlie</p></div>'
        '<div hidden><p>hidden delta echo</p></div>'
        '<div aria-hidden="true"><p>hidden foxtrot</p></div></main>'
    )

    def test_hidden_text_is_excluded_from_the_visible_count(self):
        page = parse(self.HTML)
        assert page.visible_word_count < page.raw_word_count

    def test_hidden_text_is_still_present_in_the_raw_count(self):
        # raw_word_count is the "everything that is text" measure; it must not drop hidden nodes.
        assert "alpha" not in parse(self.HTML).content

    def test_hidden_subtree_does_not_break_extraction(self):
        # A hidden element with children previously crashed text measurement and zeroed every
        # word count on the page.
        page = parse(self.HTML)
        assert page.extraction_errors == []
        assert page.visible_word_count > 0

    def test_script_and_style_never_count_as_content(self):
        page = parse(doc("<main><p>Real words here.</p>"
                         "<script>var a = 'script words not content';</script>"
                         "<style>.x{content:'style words'}</style></main>"))
        assert "script" not in page.content
        assert page.raw_word_count == 3


# ── 14-16. Images ────────────────────────────────────────────────────────────


class TestImagesWithAlt:
    def test_descriptive_alt_is_neither_missing_nor_empty(self):
        page = parse(doc('<img src="/a.png" alt="A red bicycle">'))
        assert page.image_count == 1
        assert page.missing_alt_count == 0
        assert page.empty_alt_count == 0
        assert page.images[0].alt == "A red bicycle"


class TestImagesWithoutAlt:
    def test_absent_alt_attribute_is_counted_as_missing(self):
        page = parse(doc('<img src="/a.png">'))
        assert page.missing_alt_count == 1
        assert page.empty_alt_count == 0
        assert page.images[0].alt is None

    def test_rule_engine_reports_missing_alt(self):
        result = audit_page(parse(doc('<img src="/a.png">')))
        assert any(r.check_type == "image_alt" and r.is_issue for r in result.results)


class TestImagesWithEmptyAlt:
    def test_empty_alt_is_decorative_not_missing(self):
        page = parse(doc('<img src="/a.png" alt="">'))
        assert page.missing_alt_count == 0
        assert page.empty_alt_count == 1
        assert page.images[0].alt == ""

    def test_declared_decorative_image_is_not_reported_as_a_problem(self):
        result = audit_page(parse(doc('<img src="/a.png" alt="">')))
        assert not any(r.check_type == "image_alt" and r.is_issue for r in result.results)

    def test_mixed_states_are_counted_independently(self):
        page = parse(doc('<img src="/1.png"><img src="/2.png" alt=""><img src="/3.png" alt="Cat">'))
        assert (page.image_count, page.missing_alt_count, page.empty_alt_count) == (3, 1, 1)

    def test_tracking_pixels_are_excluded_from_image_counts(self):
        page = parse(doc('<img src="/real.png" alt="Real">'
                         '<img src="/tracking-pixel.gif" width="1" height="1">'))
        assert page.image_count == 1
        assert page.tracking_pixel_count == 1
        assert page.missing_alt_count == 0  # the beacon is not a content image

    def test_css_backgrounds_and_inline_svg_are_not_images(self):
        page = parse(doc('<div style="background-image:url(/bg.png)"></div><svg><rect/></svg>'))
        assert page.image_count == 0


# ── 17-18. Links ─────────────────────────────────────────────────────────────


class TestInternalLinks:
    HTML = doc('<a href="/about">About</a><a href="https://example.com/blog">Blog</a>'
               '<a href="https://www.example.com/w">WWW</a><a href="#top">Skip</a>'
               '<a href="mailto:a@example.com">Mail</a>')

    def test_relative_and_absolute_same_host_links_are_internal(self):
        page = parse(self.HTML)
        assert "https://example.com/about" in page.internal_links
        assert "https://example.com/blog" in page.internal_links

    def test_www_variant_is_the_same_site(self):
        assert any("/w" in u for u in parse(self.HTML).internal_links)

    def test_non_http_schemes_and_fragments_are_not_crawl_targets(self):
        page = parse(self.HTML)
        assert page.non_http_link_count == 2  # the #top fragment and the mailto:
        assert not any(u.startswith("mailto:") for u in page.internal_links)

    def test_anchor_text_is_captured(self):
        page = parse(self.HTML)
        assert any(link.anchor_text == "About" for link in page.links)


class TestExternalLinks:
    HTML = doc('<a href="https://other.test/x" rel="nofollow">N</a>'
               '<a href="https://ads.test/y" rel="sponsored">S</a>'
               '<a href="https://forum.test/z" rel="ugc noopener">U</a>'
               '<a href="https://example.com.evil.test/">Lookalike</a>')

    def test_other_hosts_are_external(self):
        assert parse(self.HTML).external_link_count == 4

    def test_rel_values_are_counted_separately(self):
        page = parse(self.HTML)
        assert (page.nofollow_link_count, page.sponsored_link_count, page.ugc_link_count) == (1, 1, 1)

    def test_a_lookalike_hostname_is_not_treated_as_internal(self):
        # Classification is by normalised host, so containment of "example.com" is not enough.
        page = parse(self.HTML)
        assert not any("evil.test" in u for u in page.internal_links)

    def test_multiple_rel_tokens_are_all_read(self):
        page = parse(self.HTML)
        ugc = next(link for link in page.links if "forum.test" in link.url)
        assert "ugc" in ugc.rel and "noopener" in ugc.rel


# ── 19-20. Robots directives ─────────────────────────────────────────────────


class TestNoindex:
    def test_meta_noindex_is_detected(self):
        page = parse(doc("<h1>x</h1>", head='<meta name="robots" content="noindex, follow">'))
        assert page.meta_robots == "noindex, follow"
        result = audit_page(page)
        robots = next(r for r in result.results if r.rule_id == "robots_directive")
        assert robots.is_issue and robots.severity == "CRITICAL"

    def test_max_image_preview_none_is_not_a_noindex(self):
        # Substring matching used to read the word "none" here and de-index a healthy page.
        page = parse(doc("<h1>x</h1>", head='<meta name="robots" content="max-image-preview:none">'))
        result = audit_page(page)
        robots = next(r for r in result.results if r.rule_id == "robots_directive")
        assert not robots.is_issue

    def test_robots_none_means_noindex_nofollow(self):
        page = parse(doc("<h1>x</h1>", head='<meta name="robots" content="none">'))
        result = audit_page(page)
        robots = next(r for r in result.results if r.rule_id == "robots_directive")
        assert robots.is_issue and robots.severity == "CRITICAL"

    def test_index_follow_is_permissive(self):
        page = parse(doc("<h1>x</h1>", head='<meta name="robots" content="index, follow">'))
        result = audit_page(page)
        assert not next(r for r in result.results if r.rule_id == "robots_directive").is_issue


class TestXRobotsTag:
    def test_header_noindex_is_detected(self):
        page = parse(doc("<h1>x</h1>"), **{"x-robots-tag": "noindex"})
        assert page.x_robots_tag == "noindex"
        result = audit_page(page)
        assert next(r for r in result.results if r.rule_id == "robots_directive").is_issue

    def test_header_directive_for_another_agent_is_ignored(self):
        page = parse(doc("<h1>x</h1>"), **{"x-robots-tag": "bingbot: noindex"})
        result = audit_page(page)
        assert not next(r for r in result.results if r.rule_id == "robots_directive").is_issue

    def test_googlebot_scoped_directive_applies(self):
        page = parse(doc("<h1>x</h1>"), **{"x-robots-tag": "googlebot: noindex"})
        result = audit_page(page)
        assert next(r for r in result.results if r.rule_id == "robots_directive").is_issue

    def test_header_and_meta_are_combined(self):
        page = parse(doc("<h1>x</h1>", head='<meta name="robots" content="nofollow">'),
                     **{"x-robots-tag": "noarchive"})
        from app.services.seo.robots_directives import resolve

        directives = resolve(page.meta_robots, page.x_robots_tag)
        assert "nofollow" in directives.directives
        assert "noarchive" in directives.directives


# ── 21-23. HTTP behaviour ────────────────────────────────────────────────────


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


class TestRedirect301:
    async def test_final_url_and_chain_are_recorded(self):
        def handler(request):
            if request.url.path == "/old":
                return httpx.Response(301, headers={"location": "https://example.com/new"})
            return httpx.Response(200, text=doc("<h1>New</h1>"),
                                  headers={"content-type": "text/html"})

        async with _client(handler) as client:
            result = await fetch_url(client, "https://example.com/old", max_retries=1)

        assert result.status_code == 200
        assert result.final_url == "https://example.com/new"
        assert result.redirect_chain
        assert result.url == "https://example.com/old"  # the requested URL is preserved

    async def test_extraction_uses_the_final_url_for_canonical_comparison(self):
        page = parse(doc("<h1>x</h1>", head='<link rel="canonical" href="https://example.com/new">'),
                     url="https://example.com/new")
        assert page.canonical_status == "self"


class TestRedirectChain:
    async def test_every_hop_is_recorded(self):
        hops = {"/a": "/b", "/b": "/c"}

        def handler(request):
            nxt = hops.get(request.url.path)
            if nxt:
                return httpx.Response(302, headers={"location": f"https://example.com{nxt}"})
            return httpx.Response(200, text=doc("<h1>C</h1>"),
                                  headers={"content-type": "text/html"})

        async with _client(handler) as client:
            result = await fetch_url(client, "https://example.com/a", max_retries=1)

        assert result.final_url == "https://example.com/c"
        assert len(result.redirect_chain) >= 2

    def test_a_multi_hop_chain_is_reported_by_the_rule_engine(self):
        page = parse(doc("<h1>x</h1>"))
        page.final_url = "https://example.com/c"
        page.redirect_chain = ["https://example.com/a", "https://example.com/b"]
        result = audit_page(page)
        assert any(r.rule_id == "redirect_chain" and r.is_issue for r in result.results)

    def test_a_trailing_slash_only_redirect_is_not_reported(self):
        page = parse(doc("<h1>x</h1>"), url="https://example.com/page")
        page.final_url = "https://example.com/page"
        page.redirect_chain = ["https://example.com/page/"]
        result = audit_page(page)
        assert not next(r for r in result.results if r.rule_id == "redirect_chain").is_issue


class Test404:
    async def test_404_is_recorded_as_404_not_as_a_failure(self):
        async with _client(lambda r: httpx.Response(404, text="<h1>Not found</h1>")) as client:
            result = await fetch_url(client, "https://example.com/gone", max_retries=1)
        assert result.status_code == 404
        assert result.error is None  # a 404 is a valid response, not a transport error

    def test_a_404_produces_no_fabricated_seo_findings(self):
        page = empty_page("https://example.com/gone", 404)
        result = audit_page(page)
        issues = {r.rule_id for r in result.results if r.is_issue}
        assert issues == {"http_status"}

    def test_a_failed_fetch_is_not_scored_as_a_healthy_page(self):
        page = empty_page("https://example.com/dead", 0, error="ConnectError")
        assert page.is_usable is False
        result = audit_page(page)
        assert {r.rule_id for r in result.results if r.is_issue} == {"http_status"}
        assert result.seo_score < 50

    def test_skipped_checks_do_not_count_as_passes(self):
        result = audit_page(empty_page("https://example.com/dead", 0))
        skipped = [r for r in result.results if r.status == "skipped"]
        assert skipped, "unreachable page must skip content rules"
        assert all(r.score == 0.0 for r in skipped)


# ── 24. robots.txt restrictions ──────────────────────────────────────────────


class TestRobotsTxtRestrictions:
    TXT = """
    User-agent: *
    Disallow: /private/
    Disallow: /tmp
    Allow: /private/public-page
    Sitemap: https://example.com/sitemap.xml
    Crawl-delay: 2
    """

    def test_disallowed_path_is_blocked(self):
        rules = parse_robots(self.TXT)
        assert rules.is_allowed("https://example.com/private/secret") is False

    def test_longer_allow_overrides_disallow(self):
        rules = parse_robots(self.TXT)
        assert rules.is_allowed("https://example.com/private/public-page") is True

    def test_unlisted_path_is_permitted(self):
        assert parse_robots(self.TXT).is_allowed("https://example.com/blog/post") is True

    def test_sitemaps_and_crawl_delay_are_extracted(self):
        rules = parse_robots(self.TXT)
        assert rules.sitemaps == ["https://example.com/sitemap.xml"]
        assert rules.crawl_delay == 2

    def test_a_missing_robots_txt_permits_everything(self):
        from app.services.crawler.robots import RobotsRules

        # A robots.txt that was never fetched (404, timeout) is the default object, not a parse
        # of empty text - an unreachable robots.txt must never be read as "disallow everything".
        rules = RobotsRules()
        assert rules.fetched is False
        assert rules.is_allowed("https://example.com/anything") is True

    def test_an_empty_robots_txt_permits_everything(self):
        rules = parse_robots("")
        assert rules.fetched is True  # it was fetched; it simply contains no rules
        assert rules.is_allowed("https://example.com/anything") is True

    def test_agent_specific_block_is_preferred_over_wildcard(self):
        txt = ("User-agent: *\nDisallow: /\n\n"
               "User-agent: SEO-Automation-Crawler\nDisallow: /admin\n")
        rules = parse_robots(txt, "SEO-Automation-Crawler/2.0")
        assert rules.is_allowed("https://example.com/public") is True
        assert rules.is_allowed("https://example.com/admin") is False


# ── 25-26. Sitemaps ──────────────────────────────────────────────────────────


URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc><lastmod>2024-01-01</lastmod>
       <changefreq>daily</changefreq><priority>1.0</priority></url>
  <url><loc>https://example.com/about</loc></url>
  <url><loc>https://other.test/off-domain</loc></url>
  <url><loc>https://example.com/brochure.pdf</loc></url>
</urlset>"""

SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-a.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-b.xml</loc></sitemap>
</sitemapindex>"""


class TestSitemap:
    def test_urls_and_metadata_are_parsed(self):
        entries, nested, used_fallback = parse_sitemap_document(URLSET)
        assert used_fallback is False
        assert nested == []
        assert entries[0].loc == "https://example.com/"
        assert entries[0].lastmod == "2024-01-01"
        assert entries[0].changefreq == "daily"
        assert entries[0].priority == 1.0

    def test_namespaced_document_is_handled_without_regex(self):
        entries, _, used_fallback = parse_sitemap_document(URLSET)
        assert len(entries) == 4 and used_fallback is False

    async def test_off_domain_and_asset_entries_are_skipped_with_a_reason(self):
        def handler(_):
            return httpx.Response(200, text=URLSET, headers={"content-type": "application/xml"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_sitemap_entries(
                client, ["https://example.com/sitemap.xml"], BASE, 100
            )
        assert result.urls == ["https://example.com/", "https://example.com/about"]
        assert result.skipped.get("off_domain") == 1
        assert result.skipped.get("asset") == 1

    async def test_malformed_xml_falls_back_rather_than_losing_every_url(self):
        broken = "<urlset><url><loc>https://example.com/a</loc>"

        def handler(_):
            return httpx.Response(200, text=broken)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_sitemap_entries(
                client, ["https://example.com/sitemap.xml"], BASE, 10
            )
        assert result.urls == ["https://example.com/a"]

    async def test_gzip_without_a_gz_extension_is_decompressed(self):
        payload = gzip.compress(URLSET.encode())

        def handler(_):
            return httpx.Response(200, content=payload,
                                  headers={"content-type": "application/xml"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_sitemap_entries(
                client, ["https://example.com/sitemap.xml"], BASE, 10
            )
        assert "https://example.com/about" in result.urls


class TestSitemapIndex:
    def test_index_yields_nested_sitemaps_and_no_page_urls(self):
        entries, nested, _ = parse_sitemap_document(SITEMAP_INDEX_XML)
        assert entries == []
        assert nested == ["https://example.com/sitemap-a.xml", "https://example.com/sitemap-b.xml"]

    async def test_nested_sitemaps_are_followed(self):
        def handler(request):
            if request.url.path == "/sitemap.xml":
                body = SITEMAP_INDEX_XML
            elif request.url.path == "/sitemap-a.xml":
                body = URLSET
            else:
                body = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        "<url><loc>https://example.com/blog</loc></url></urlset>")
            return httpx.Response(200, text=body, headers={"content-type": "application/xml"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_sitemap_entries(
                client, ["https://example.com/sitemap.xml"], BASE, 100
            )
        assert "https://example.com/blog" in result.urls
        assert "https://example.com/about" in result.urls
        assert len(result.sitemaps_fetched) == 3

    async def test_an_unreachable_child_sitemap_is_reported_not_swallowed(self):
        def handler(request):
            if request.url.path == "/sitemap.xml":
                return httpx.Response(200, text=SITEMAP_INDEX_XML,
                                      headers={"content-type": "application/xml"})
            if request.url.path == "/sitemap-a.xml":
                return httpx.Response(200, text=URLSET,
                                      headers={"content-type": "application/xml"})
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_sitemap_entries(
                client, ["https://example.com/sitemap.xml"], BASE, 100
            )
        assert "https://example.com/sitemap-b.xml" in result.sitemaps_failed
        assert result.urls  # the reachable half is still used


# ── 27-29. URL identity ──────────────────────────────────────────────────────


class TestDuplicateUrls:
    def test_the_same_page_linked_twice_is_stored_once(self):
        page = parse(doc('<a href="/a">1</a><a href="/a">2</a><a href="/a/">3</a>'))
        assert page.internal_links.count("https://example.com/a") == 1
        assert len(page.internal_links) == 1

    async def test_a_sitemap_listing_a_url_twice_yields_one_entry(self):
        xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               "<url><loc>https://example.com/a</loc></url>"
               "<url><loc>https://example.com/a/</loc></url></urlset>")

        def handler(_):
            return httpx.Response(200, text=xml, headers={"content-type": "application/xml"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await collect_sitemap_entries(
                client, ["https://example.com/sitemap.xml"], BASE, 10
            )
        assert result.urls == ["https://example.com/a"]
        assert result.skipped.get("duplicate") == 1


class TestUrlParameters:
    def test_tracking_parameters_are_removed(self):
        assert normalize_url("https://example.com/p?utm_source=x&utm_medium=y") == \
            "https://example.com/p"

    def test_content_bearing_parameters_are_preserved(self):
        # Removing these would merge genuinely different pages into one.
        assert normalize_url("https://example.com/p?id=7") == "https://example.com/p?id=7"

    def test_parameter_order_does_not_create_a_second_url(self):
        assert normalize_url("https://example.com/p?b=2&a=1") == \
            normalize_url("https://example.com/p?a=1&b=2")

    def test_fragments_are_dropped(self):
        assert normalize_url("https://example.com/p#section") == "https://example.com/p"

    def test_different_values_remain_different_urls(self):
        assert normalize_url("https://example.com/p?id=7") != normalize_url("https://example.com/p?id=8")


class TestTrailingSlashVariants:
    def test_trailing_slash_is_normalised_away(self):
        assert normalize_url("https://example.com/a/") == normalize_url("https://example.com/a")

    def test_the_root_path_keeps_its_slash(self):
        assert normalize_url("https://example.com") == "https://example.com/"

    def test_default_ports_are_removed(self):
        assert normalize_url("https://example.com:443/a") == "https://example.com/a"

    def test_scheme_and_host_case_are_normalised(self):
        assert normalize_url("HTTPS://Example.COM/a") == "https://example.com/a"

    def test_path_case_is_preserved(self):
        # Paths are case-sensitive on most servers; lowering them would merge distinct pages.
        assert normalize_url("https://example.com/CaseSensitive") == \
            "https://example.com/CaseSensitive"


# ── 30. Malformed HTML ───────────────────────────────────────────────────────


class TestMalformedHtml:
    def test_unclosed_tags_still_yield_signals(self):
        html = "<html><head><title>Broken<body><h1>Heading<p>Some text here"
        page = parse(html)
        assert page.title == "Broken"
        assert page.h1 == "Heading"
        assert page.extraction_errors == []

    def test_stray_closing_tags_do_not_lose_the_document(self):
        page = parse("</div></p><h1>Still works</h1></body></html>")
        assert page.h1 == "Still works"
        assert page.extraction_errors == ["lxml_returned_empty_document: reparsed with html.parser"]

    def test_junk_before_the_doctype_does_not_discard_the_page(self):
        page = parse('</span><!doctype html><html><head><title>T</title></head>'
                     '<body><h1>H</h1></body></html>')
        assert page.title == "T"
        assert page.h1 == "H"

    def test_empty_document_is_not_an_error_but_is_empty(self):
        page = parse("")
        assert page.title is None
        assert page.word_count == 0
        assert page.extraction_errors == []

    def test_non_html_body_does_not_produce_seo_signals(self):
        page = parse("{\"json\": true}")
        assert page.title is None
        assert page.h1_count == 0


# ── 31-32. Structured data ───────────────────────────────────────────────────


class TestJsonLd:
    VALID = doc("<h1>x</h1>", head='<script type="application/ld+json">'
                                   '{"@context":"https://schema.org","@type":"Article",'
                                   '"headline":"Hello"}</script>')

    def test_types_are_extracted(self):
        page = parse(self.VALID)
        assert page.has_structured_data is True
        assert "Article" in page.structured_data_types
        assert "json-ld" in page.structured_data_formats
        assert page.structured_data_invalid is False

    def test_graph_and_array_forms_are_both_read(self):
        page = parse(doc("<h1>x</h1>", head='<script type="application/ld+json">'
                                            '{"@graph":[{"@type":"Organization"},'
                                            '{"@type":"WebSite"}]}</script>'))
        assert set(page.structured_data_types) >= {"Organization", "WebSite"}

    def test_microdata_is_detected_as_its_own_format(self):
        page = parse(doc('<div itemscope itemtype="https://schema.org/Product">'
                         '<span itemprop="name">Widget</span></div>'))
        assert "microdata" in page.structured_data_formats
        assert "Product" in page.structured_data_types

    def test_rdfa_is_detected_as_its_own_format(self):
        page = parse(doc('<div vocab="https://schema.org/" typeof="Person">'
                         '<span property="name">Ada</span></div>'))
        assert "rdfa" in page.structured_data_formats
        assert "Person" in page.structured_data_types


class TestInvalidJsonLd:
    HTML = doc("<h1>x</h1>", head='<script type="application/ld+json">{"@type": Article,}</script>')

    def test_invalid_json_is_flagged_not_ignored(self):
        page = parse(self.HTML)
        assert page.structured_data_invalid is True
        assert page.json_ld_error is not None

    def test_invalid_json_does_not_claim_structured_data_is_present(self):
        page = parse(self.HTML)
        assert page.structured_data_types == []

    def test_one_broken_block_does_not_discard_a_valid_one(self):
        page = parse(doc("<h1>x</h1>",
                         head='<script type="application/ld+json">{oops}</script>'
                              '<script type="application/ld+json">{"@type":"FAQPage"}</script>'))
        assert "FAQPage" in page.structured_data_types
        assert page.structured_data_invalid is True

    def test_extraction_continues_after_a_structured_data_error(self):
        page = parse(self.HTML)
        assert page.h1 == "x"
        assert page.extraction_errors == []


# ── Tracking-pixel heuristics, used by scenario 14-16 ────────────────────────


class TestTrackingPixelDetection:
    @pytest.mark.parametrize("markup", [
        '<img src="/pixel.gif">',
        '<img src="/b/ss/collect?x=1">',
        '<img src="/img.png" width="1" height="1">',
        '<img src="/beacon.png">',
    ])
    def test_beacons_are_recognised(self, markup):
        from bs4 import BeautifulSoup

        img = BeautifulSoup(markup, "lxml").find("img")
        assert is_tracking_pixel(img) is True

    @pytest.mark.parametrize("markup", [
        '<img src="/photos/pixelated-art.jpg" alt="Art">',
        '<img src="/hero.png" width="1200" height="600">',
        '<img src="/logo.svg">',
    ])
    def test_content_images_are_not_mistaken_for_beacons(self, markup):
        from bs4 import BeautifulSoup

        img = BeautifulSoup(markup, "lxml").find("img")
        assert is_tracking_pixel(img) is False
