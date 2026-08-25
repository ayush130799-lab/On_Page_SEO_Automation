"""Crawler internals: robots parsing, sitemap discovery, fetching, extraction and orchestration."""

from __future__ import annotations

import httpx
import pytest

from app.services.crawler import (
    CrawlConfig,
    Crawler,
    HostRateLimiter,
    collect_sitemap_urls,
    extract_page,
    fetch_robots,
    fetch_url,
    needs_rendering,
    parse_robots,
)
from app.utils.url_utils import (
    absolute_url,
    content_hash,
    domain_of,
    is_probably_page,
    is_same_domain,
    matches_any_pattern,
    normalize_url,
    url_hash,
    url_path,
)

# ── URL utilities ───────────────────────────────────────────────────────────


class TestUrlUtils:
    def test_normalisation(self):
        assert normalize_url("HTTPS://Example.com/about/") == "https://example.com/about"
        assert normalize_url("https://example.com") == "https://example.com/"
        assert normalize_url("https://example.com/a#section") == "https://example.com/a"
        assert normalize_url("http://example.com:80/a") == "http://example.com/a"
        assert normalize_url("https://example.com:443/a") == "https://example.com/a"

    def test_tracking_parameters_are_stripped(self):
        assert (
            normalize_url("https://example.com/p?utm_source=x&id=7&gclid=z")
            == "https://example.com/p?id=7"
        )

    def test_query_parameters_are_sorted_for_stable_identity(self):
        assert normalize_url("https://example.com/p?b=2&a=1") == normalize_url(
            "https://example.com/p?a=1&b=2"
        )

    def test_url_hash_is_stable_across_equivalent_forms(self):
        assert url_hash("https://Example.com/about/") == url_hash("https://example.com/about")
        assert url_hash("https://example.com/a") != url_hash("https://example.com/b")

    def test_url_path_extraction(self):
        assert url_path("https://example.com/blog/post") == "/blog/post"
        assert url_path("https://example.com") == "/"
        assert url_path("https://example.com/s?q=1") == "/s?q=1"

    def test_content_hash_ignores_whitespace_and_case(self):
        assert content_hash("Hello   World") == content_hash("hello world")
        assert content_hash("") is None
        assert content_hash(None) is None

    def test_domain_comparison_ignores_www(self):
        assert domain_of("https://www.example.com/a") == "www.example.com"
        assert is_same_domain("https://www.example.com/a", "example.com") is True
        assert is_same_domain("https://other.com/a", "example.com") is False

    def test_asset_urls_are_not_pages(self):
        assert is_probably_page("https://example.com/logo.png") is False
        assert is_probably_page("https://example.com/app.js") is False
        assert is_probably_page("https://example.com/report.pdf") is False
        assert is_probably_page("https://example.com/about") is True
        assert is_probably_page("https://example.com/v1.2/docs") is True

    def test_absolute_url_skips_non_navigable_hrefs(self):
        assert absolute_url("https://example.com/a", "/b") == "https://example.com/b"
        for href in ("#top", "mailto:x@y.com", "tel:+1", "javascript:void(0)", "data:text/x,y", ""):
            assert absolute_url("https://example.com/a", href) is None

    def test_include_exclude_patterns(self):
        assert matches_any_pattern("https://example.com/blog/post", ["/blog/*"]) is True
        assert matches_any_pattern("https://example.com/about", ["/blog/*"]) is False
        assert matches_any_pattern("https://example.com/x", None) is False


# ── robots.txt ──────────────────────────────────────────────────────────────


class TestRobots:
    def test_disallow_and_allow_precedence(self):
        rules = parse_robots(
            """
            User-agent: *
            Disallow: /admin/
            Disallow: /private
            Allow: /admin/public
            Sitemap: https://example.com/sitemap.xml
            """
        )
        assert rules.is_allowed("https://example.com/") is True
        assert rules.is_allowed("https://example.com/admin/settings") is False
        # The longer Allow rule wins over the shorter Disallow.
        assert rules.is_allowed("https://example.com/admin/public/page") is True
        assert rules.is_allowed("https://example.com/private") is False
        assert rules.sitemaps == ["https://example.com/sitemap.xml"]

    def test_wildcard_and_end_anchor_patterns(self):
        rules = parse_robots(
            """
            User-agent: *
            Disallow: /*.pdf$
            Disallow: /search?
            """
        )
        assert rules.is_allowed("https://example.com/files/report.pdf") is False
        assert rules.is_allowed("https://example.com/files/report.pdf.html") is True
        assert rules.is_allowed("https://example.com/search?q=x") is False

    def test_empty_disallow_means_allow_everything(self):
        rules = parse_robots("User-agent: *\nDisallow:")
        assert rules.is_allowed("https://example.com/anything") is True

    def test_a_named_agent_block_beats_the_wildcard(self):
        rules = parse_robots(
            """
            User-agent: *
            Disallow: /

            User-agent: SEO-Automation-Crawler
            Disallow: /admin
            """,
            user_agent="SEO-Automation-Crawler/2.0",
        )
        assert rules.is_allowed("https://example.com/public") is True
        assert rules.is_allowed("https://example.com/admin") is False

    def test_crawl_delay_is_read(self):
        assert parse_robots("User-agent: *\nCrawl-delay: 2.5").crawl_delay == 2.5

    def test_comments_are_ignored(self):
        rules = parse_robots("# comment\nUser-agent: *\nDisallow: /x  # trailing")
        assert rules.is_allowed("https://example.com/x") is False

    async def test_missing_robots_is_permissive(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(404))
        async with httpx.AsyncClient(transport=transport) as client:
            rules = await fetch_robots(client, "https://example.com")
        assert rules.fetched is False
        assert rules.is_allowed("https://example.com/anything") is True


# ── Sitemaps ────────────────────────────────────────────────────────────────


SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-posts.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_PAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc></url>
  <url><loc>https://example.com/about</loc></url>
  <url><loc>https://other-domain.com/spam</loc></url>
  <url><loc>https://example.com/brochure.pdf</loc></url>
</urlset>"""

SITEMAP_POSTS = """<?xml version="1.0"?>
<urlset><url><loc>https://example.com/blog/one</loc></url></urlset>"""


class TestSitemaps:
    def _transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = {
                "/sitemap.xml": SITEMAP_INDEX,
                "/sitemap-pages.xml": SITEMAP_PAGES,
                "/sitemap-posts.xml": SITEMAP_POSTS,
            }.get(request.url.path)
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, text=body, headers={"content-type": "application/xml"})

        return httpx.MockTransport(handler)

    async def test_index_is_followed_recursively(self):
        async with httpx.AsyncClient(transport=self._transport()) as client:
            urls = await collect_sitemap_urls(
                client, ["https://example.com/sitemap.xml"], "example.com", 100
            )
        assert "https://example.com/" in urls
        assert "https://example.com/about" in urls
        assert "https://example.com/blog/one" in urls

    async def test_off_domain_and_asset_urls_are_dropped(self):
        async with httpx.AsyncClient(transport=self._transport()) as client:
            urls = await collect_sitemap_urls(
                client, ["https://example.com/sitemap.xml"], "example.com", 100
            )
        assert not any("other-domain.com" in u for u in urls)
        assert not any(u.endswith(".pdf") for u in urls)

    async def test_max_urls_is_respected(self):
        async with httpx.AsyncClient(transport=self._transport()) as client:
            urls = await collect_sitemap_urls(
                client, ["https://example.com/sitemap.xml"], "example.com", 2
            )
        assert len(urls) <= 2

    async def test_gzipped_sitemap_is_decompressed(self):
        import gzip

        payload = gzip.compress(SITEMAP_POSTS.encode())

        def handler(_):
            return httpx.Response(200, content=payload, headers={"content-type": "application/gzip"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            urls = await collect_sitemap_urls(
                client, ["https://example.com/sitemap.xml.gz"], "example.com", 10
            )
        assert urls == ["https://example.com/blog/one"]

    async def test_a_broken_sitemap_does_not_raise(self):
        def handler(_):
            return httpx.Response(200, text="<urlset><url><loc>https://example.com/a</loc>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            urls = await collect_sitemap_urls(
                client, ["https://example.com/sitemap.xml"], "example.com", 10
            )
        assert urls == ["https://example.com/a"]


# ── Fetching ────────────────────────────────────────────────────────────────


class TestFetcher:
    async def test_redirect_chain_is_captured(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(301, headers={"location": "https://example.com/middle"})
            if request.url.path == "/middle":
                return httpx.Response(302, headers={"location": "https://example.com/final"})
            return httpx.Response(200, text="<html><body>done</body></html>",
                                  headers={"content-type": "text/html"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            result = await fetch_url(client, "https://example.com/start")

        assert result.status_code == 200
        assert result.final_url == "https://example.com/final"
        assert len(result.redirect_chain) == 2
        assert result.redirected is True

    async def test_transport_failures_are_retried_then_recorded(self):
        attempts = {"count": 0}

        def handler(_):
            attempts["count"] += 1
            raise httpx.ConnectError("refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_url(client, "https://example.com/x", max_retries=3)

        assert attempts["count"] == 3
        assert result.status_code == 0
        assert "ConnectError" in result.error

    async def test_retryable_status_is_retried_and_success_is_returned(self):
        attempts = {"count": 0}

        def handler(_):
            attempts["count"] += 1
            if attempts["count"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, text="<html>ok</html>",
                                  headers={"content-type": "text/html"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_url(client, "https://example.com/x", max_retries=4)

        assert result.status_code == 200
        assert result.attempts == 3

    async def test_404_is_not_retried(self):
        attempts = {"count": 0}

        def handler(_):
            attempts["count"] += 1
            return httpx.Response(404, text="missing")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_url(client, "https://example.com/gone", max_retries=3)

        assert attempts["count"] == 1
        assert result.status_code == 404
        assert result.ok is False

    async def test_non_html_bodies_are_not_kept(self):
        def handler(_):
            return httpx.Response(200, text='{"a":1}', headers={"content-type": "application/json"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_url(client, "https://example.com/api")

        assert result.html == ""
        assert result.is_html is False

    async def test_rate_limiter_spaces_requests_per_host(self):
        import time

        limiter = HostRateLimiter(requests_per_second=50)
        started = time.monotonic()
        for _ in range(4):
            await limiter.acquire("example.com")
        assert time.monotonic() - started >= 0.05


# ── Rendering decision ──────────────────────────────────────────────────────


class TestRenderDecision:
    def test_never_and_always_short_circuit(self):
        rich = "<html><body>" + ("content " * 200) + "</body></html>"
        assert needs_rendering(rich, render_mode="always") is True
        assert needs_rendering("", render_mode="never") is False

    def test_empty_spa_root_triggers_rendering(self):
        assert needs_rendering('<html><body><div id="root"></div></body></html>') is True
        assert needs_rendering('<html><body><div id="__next"></div></body></html>') is True

    def test_thin_body_triggers_rendering(self):
        assert needs_rendering("<html><body><p>Hi</p></body></html>") is True

    def test_a_content_rich_page_is_not_rendered(self):
        html = "<html><body>" + ("Real readable content. " * 60) + "</body></html>"
        assert needs_rendering(html) is False

    def test_script_text_does_not_count_as_content(self):
        html = "<html><body><script>" + ("x=1;" * 300) + "</script><p>Hi</p></body></html>"
        assert needs_rendering(html) is True


# ── Extraction ──────────────────────────────────────────────────────────────


RICH_HTML = """<!DOCTYPE html>
<html lang="en-GB">
<head>
  <title>Running Shoes &amp; Trail Gear | Example</title>
  <meta name="description" content="Find the best running shoes and trail gear.">
  <meta name="robots" content="index, follow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta property="og:title" content="Running Shoes">
  <meta name="twitter:card" content="summary">
  <link rel="canonical" href="/shoes">
  <link rel="alternate" hreflang="fr" href="https://example.com/fr/shoes">
  <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"Shoe",
     "offers":{"@type":"Offer","price":"99"}}
  </script>
</head>
<body>
  <h1>Best Running Shoes</h1>
  <h2>Trail</h2><h2>Road</h2><h3>Beginners</h3>
  <p>A comprehensive guide to choosing shoes with the right cushioning and support.</p>
  <img src="/a.jpg" alt="Trail shoe" width="100" height="80">
  <img src="/b.jpg">
  <a href="/shoes/trail">Trail</a>
  <a href="/shoes/trail">Trail again</a>
  <a href="https://external.com/x" rel="nofollow">External</a>
  <script>var tracking = "should not appear in content";</script>
</body>
</html>"""


class TestExtractor:
    def _page(self):
        return extract_page("https://example.com/shoes", RICH_HTML, "example.com", 200)

    def test_core_fields(self):
        page = self._page()
        assert page.title == "Running Shoes & Trail Gear | Example"
        assert page.meta_description.startswith("Find the best")
        assert page.h1 == "Best Running Shoes"
        assert page.h1_count == 1
        assert page.h2_count == 2
        assert page.h3_count == 1
        assert page.lang == "en-GB"
        assert page.has_viewport is True

    def test_relative_canonical_is_resolved(self):
        assert self._page().canonical_url == "https://example.com/shoes"

    def test_structured_data_types_are_collected_including_nested(self):
        page = self._page()
        assert page.has_structured_data is True
        assert "Product" in page.structured_data_types
        assert "Offer" in page.structured_data_types
        assert page.structured_data_invalid is False

    def test_invalid_json_ld_is_flagged(self):
        html = '<html><head><script type="application/ld+json">{oops}</script></head><body>x</body></html>'
        page = extract_page("https://example.com/x", html, "example.com", 200)
        assert page.structured_data_invalid is True

    def test_links_are_split_deduplicated_and_nofollow_counted(self):
        page = self._page()
        assert page.internal_links == ["https://example.com/shoes/trail"]
        assert page.external_links == ["https://external.com/x"]
        assert page.nofollow_link_count == 1

    def test_images_and_alt_text(self):
        page = self._page()
        assert page.image_count == 2
        assert page.missing_alt_count == 1
        assert page.images_without_dimensions == 1

    def test_script_content_is_excluded_from_text(self):
        page = self._page()
        assert "should not appear" not in page.content
        assert "comprehensive guide" in page.content
        assert page.word_count > 0
        assert page.content_hash is not None

    def test_social_metadata(self):
        page = self._page()
        assert page.has_open_graph is True
        assert page.has_twitter_card is True

    def test_hreflang_alternates(self):
        assert self._page().hreflang == [{"lang": "fr", "href": "https://example.com/fr/shoes"}]

    def test_empty_html_does_not_crash(self):
        page = extract_page("https://example.com/x", "", "example.com", 200)
        assert page.title is None
        assert page.word_count == 0


# ── Orchestration ───────────────────────────────────────────────────────────


SITE = {
    "/": '<html><head><title>Home Page Of The Example Site</title></head><body><h1>Home</h1>'
         '<a href="/about">About</a><a href="/blog">Blog</a><a href="/gone">Gone</a>'
         '<p>Welcome to the site.</p></body></html>',
    "/about": '<html><head><title>About Us At Example Company</title></head><body><h1>About</h1>'
              '<a href="/">Home</a><p>About text.</p></body></html>',
    "/blog": '<html><head><title>Blog Index For Example Company</title></head><body><h1>Blog</h1>'
             '<a href="/">Home</a><p>Posts.</p></body></html>',
    "/orphan": '<html><head><title>Orphan Page Nobody Links To</title></head><body>'
               '<h1>Orphan</h1><p>Lonely.</p></body></html>',
}


def site_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /admin\nSitemap: https://example.com/sitemap.xml",
            )
        if path == "/sitemap.xml":
            locs = "".join(
                f"<url><loc>https://example.com{p}</loc></url>" for p in [*SITE, "/orphan"]
            )
            return httpx.Response(
                200, text=f"<urlset>{locs}</urlset>",
                headers={"content-type": "application/xml"},
            )
        if path == "/gone":
            return httpx.Response(404, text="<html><body>Not found</body></html>",
                                  headers={"content-type": "text/html"})
        if path in SITE:
            return httpx.Response(200, text=SITE[path], headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestCrawler:
    def _config(self, **overrides) -> CrawlConfig:
        base = CrawlConfig(
            max_pages=50,
            concurrency=4,
            render_enabled=False,
            allow_local=True,
            crawl_delay=0,
            rate_limit_per_second=1000,
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    async def _run(self, config=None, monkeypatch=None):
        crawler = Crawler("https://example.com/", config or self._config())
        transport = site_transport()

        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        httpx.AsyncClient.__init__ = patched
        try:
            return await crawler.run()
        finally:
            httpx.AsyncClient.__init__ = original

    async def test_crawl_discovers_via_sitemap_and_links(self):
        result = await self._run()
        urls = {p.final_url or p.url for p in result.pages}
        assert "https://example.com/" in urls
        assert "https://example.com/about" in urls
        assert "https://example.com/blog" in urls
        assert "https://example.com/orphan" in urls  # sitemap-only, never linked

    async def test_a_404_page_is_recorded_not_dropped(self):
        result = await self._run()
        gone = [p for p in result.pages if p.url.endswith("/gone")]
        assert len(gone) == 1
        assert gone[0].status_code == 404

    async def test_broken_and_inbound_link_counts_are_derived_from_the_crawl(self):
        result = await self._run()
        home = next(p for p in result.pages if (p.final_url or p.url) == "https://example.com/")
        about = next(p for p in result.pages if (p.final_url or p.url).endswith("/about"))
        orphan = next(p for p in result.pages if (p.final_url or p.url).endswith("/orphan"))

        assert home.broken_link_count == 1  # /gone returns 404
        assert about.inbound_internal_links >= 1
        assert orphan.inbound_internal_links == 0

    async def test_max_pages_caps_the_crawl(self):
        result = await self._run(self._config(max_pages=2))
        assert result.pages_crawled <= 2

    async def test_exclude_patterns_are_honoured(self):
        result = await self._run(self._config(exclude_patterns=["/blog*"]))
        assert not any((p.final_url or p.url).endswith("/blog") for p in result.pages)

    async def test_include_patterns_restrict_the_crawl(self):
        result = await self._run(self._config(include_patterns=["/about"]))
        paths = {(p.final_url or p.url) for p in result.pages}
        assert paths == {"https://example.com/about"}

    async def test_incremental_mode_crawls_only_the_targets(self):
        config = self._config(
            target_urls=["https://example.com/about"], follow_links=False
        )
        result = await self._run(config)
        assert [p.final_url or p.url for p in result.pages] == ["https://example.com/about"]

    async def test_non_public_targets_are_refused(self):
        crawler = Crawler("https://example.com/", self._config(allow_local=False))
        crawler.start_url = "file:///etc/passwd"
        with pytest.raises(ValueError):
            await crawler.run()


class TestRenderingIntegration:
    """The renderer must be consulted for thin pages and skipped for rich ones."""

    def _spa_transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            if request.url.path.endswith(".xml"):
                return httpx.Response(404)
            if request.url.path == "/rich":
                body = "<html><body><h1>Rich</h1><p>" + ("real content " * 100) + "</p></body></html>"
            else:
                body = '<html><head><title>App</title></head><body><div id="root"></div></body></html>'
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})

        return httpx.MockTransport(handler)

    async def _crawl(self, monkeypatch, render_mode="auto"):
        rendered_urls: list[str] = []

        async def fake_render(self, url, user_agent=None):
            rendered_urls.append(url)
            self.rendered_count += 1
            return (
                '<html><head><title>App Shell With Real Content Now</title></head><body>'
                '<h1>Hydrated</h1><p>' + ("client rendered content " * 60) + "</p></body></html>"
            )

        monkeypatch.setattr(
            "app.services.crawler.renderer.PlaywrightRenderer.render", fake_render
        )

        config = CrawlConfig(
            max_pages=5, concurrency=2, render_enabled=True, render_mode=render_mode,
            allow_local=True, crawl_delay=0, rate_limit_per_second=1000,
        )
        crawler = Crawler("https://spa.test/", config)
        transport = self._spa_transport()
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        httpx.AsyncClient.__init__ = patched
        try:
            result = await crawler.run()
        finally:
            httpx.AsyncClient.__init__ = original
        return result, rendered_urls

    async def test_thin_spa_shell_is_rendered_and_content_recovered(self, monkeypatch):
        result, rendered = await self._crawl(monkeypatch)
        assert rendered == ["https://spa.test/"]
        page = result.pages[0]
        assert page.was_rendered is True
        assert page.h1 == "Hydrated"
        assert page.word_count > 50
        assert result.pages_rendered == 1

    async def test_render_mode_never_skips_the_browser_entirely(self, monkeypatch):
        result, rendered = await self._crawl(monkeypatch, render_mode="never")
        assert rendered == []
        assert result.pages[0].was_rendered is False

    async def test_render_failure_falls_back_to_static_html(self, monkeypatch):
        async def failing_render(self, url, user_agent=None):
            return None

        monkeypatch.setattr(
            "app.services.crawler.renderer.PlaywrightRenderer.render", failing_render
        )
        config = CrawlConfig(
            max_pages=2, concurrency=1, render_enabled=True, allow_local=True,
            crawl_delay=0, rate_limit_per_second=1000,
        )
        crawler = Crawler("https://spa.test/", config)
        transport = self._spa_transport()
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        httpx.AsyncClient.__init__ = patched
        try:
            result = await crawler.run()
        finally:
            httpx.AsyncClient.__init__ = original

        # The crawl still produces a page, just an un-hydrated one.
        assert result.pages
        assert result.pages[0].was_rendered is False
        assert result.pages[0].title == "App"
