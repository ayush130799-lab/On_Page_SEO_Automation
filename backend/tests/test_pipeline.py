"""End-to-end: crawl a mock website, persist pages/audits/issues, expose them through the API."""

from __future__ import annotations

import httpx
import pytest

from app.models import CrawlMode, CrawlRun, Page, RunStatus, SEOAudit, SEOIssue, Severity, Website
from app.services.pipeline import create_crawl_run, run_crawl_pipeline
from app.utils.url_utils import url_hash

from .conftest import auth_headers

# A small site with deliberate, known SEO defects.
PAGES = {
    "/": """<html lang="en"><head>
        <title>Acme Widgets — Industrial Fasteners And Fittings</title>
        <meta name="description" content="Acme supplies industrial fasteners, fittings and widgets to manufacturers across the country.">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="canonical" href="https://acme.test/">
        <meta property="og:title" content="Acme Widgets">
        <script type="application/ld+json">{"@type":"Organization","name":"Acme"}</script>
        </head><body><h1>Acme Widgets</h1><h2>Products</h2><h3>Fasteners</h3>
        <a href="/products">Products</a><a href="/about">About</a><a href="/broken">Broken</a>
        <img src="/hero.jpg" alt="Factory floor" width="800" height="400">
        <p>%s</p></body></html>""" % ("Acme has supplied industrial fasteners since 1974. " * 30),

    # Missing title, missing meta description, no H1, thin content, image without alt.
    "/products": """<html><head>
        <link rel="canonical" href="https://acme.test/products"></head>
        <body><a href="/">Home</a><img src="/p.jpg"><p>Products.</p></body></html>""",

    # noindex — a CRITICAL issue on an otherwise reasonable page.
    "/about": """<html lang="en"><head>
        <title>About Acme Widgets And Our Manufacturing History</title>
        <meta name="description" content="Acme has manufactured industrial fasteners and fittings for over fifty years from its plant.">
        <meta name="robots" content="noindex, follow">
        <meta name="viewport" content="width=device-width">
        <link rel="canonical" href="https://acme.test/about">
        </head><body><h1>About</h1><h2>History</h2>
        <a href="/">Home</a><p>%s</p></body></html>""" % ("Our history spans five decades. " * 40),
}


def acme_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nSitemap: https://acme.test/sitemap.xml")
        if path == "/sitemap.xml":
            locs = "".join(f"<url><loc>https://acme.test{p}</loc></url>" for p in PAGES)
            return httpx.Response(
                200, text=f"<urlset>{locs}</urlset>",
                headers={"content-type": "application/xml"},
            )
        if path == "/broken":
            return httpx.Response(404, text="<html><body>Gone</body></html>",
                                  headers={"content-type": "text/html"})
        if path in PAGES:
            return httpx.Response(200, text=PAGES[path], headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def acme(db, member_user):
    site = Website(
        name="Acme",
        url="https://acme.test/",
        domain="acme.test",
        created_by_id=member_user.id,
        max_pages=50,
        render_mode="never",
    )
    db.add(site)
    db.flush()
    from app.models import MemberRole, WebsiteMember

    db.add(WebsiteMember(website_id=site.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(site)
    return site


async def run_pipeline(db, crawl_run_id):
    """Run the pipeline with every outbound request served by the mock site."""
    transport = acme_transport()
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched
    try:
        return await run_crawl_pipeline(db, crawl_run_id)
    finally:
        httpx.AsyncClient.__init__ = original


@pytest.fixture
async def crawled(db, acme, monkeypatch):
    monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
    monkeypatch.setattr("app.config.settings.render_enabled", False)
    run = create_crawl_run(db, acme)
    outcome = await run_pipeline(db, run.id)
    db.expire_all()
    return outcome


class TestPipelinePersistence:
    async def test_crawl_run_completes_with_counters(self, db, crawled):
        run = db.get(CrawlRun, crawled.crawl_run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.progress_percent == 100.0
        assert run.stage == "completed"
        assert run.pages_crawled >= 4
        assert run.average_seo_score is not None
        assert run.duration_seconds is not None

    async def test_every_crawled_url_becomes_a_page(self, db, acme, crawled):
        paths = {p.path for p in db.query(Page).filter(Page.website_id == acme.id)}
        assert {"/", "/products", "/about", "/broken"} <= paths

    async def test_pages_carry_their_audit_snapshot(self, db, acme, crawled):
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        assert home.status_code == 200
        assert home.title.startswith("Acme Widgets")
        assert home.seo_score is not None
        assert home.content_hash is not None
        assert home.word_count > 50
        assert home.has_structured_data is True
        assert home.crawl_status == "crawled"

    async def test_one_audit_row_per_page_per_run(self, db, acme, crawled):
        pages = db.query(Page).filter(Page.website_id == acme.id).count()
        audits = (
            db.query(SEOAudit).filter(SEOAudit.crawl_run_id == crawled.crawl_run_id).count()
        )
        assert audits == pages

    async def test_audit_stores_checks_and_the_weight_vector(self, db, crawled):
        audit = db.query(SEOAudit).first()
        assert audit.checks and len(audit.checks) >= 20
        assert audit.weights_snapshot["content"] > 0
        assert {"rule_id", "check", "status", "score"} <= set(audit.checks[0])

    async def test_known_defects_are_detected_on_the_right_pages(self, db, acme, crawled):
        products = db.query(Page).filter(
            Page.website_id == acme.id, Page.path == "/products"
        ).one()
        rules = {
            i.rule_id for i in db.query(SEOIssue).filter(SEOIssue.page_id == products.id)
        }
        assert {"title", "meta_description", "h1", "content_length", "image_alt"} <= rules

    async def test_noindex_page_is_marked_critical(self, db, acme, crawled):
        about = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/about").one()
        assert about.highest_severity == Severity.CRITICAL
        issue = (
            db.query(SEOIssue)
            .filter(SEOIssue.page_id == about.id, SEOIssue.rule_id == "robots_directive")
            .one()
        )
        assert issue.severity == Severity.CRITICAL

    async def test_broken_internal_link_is_counted_on_the_linking_page(self, db, acme, crawled):
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        assert home.broken_link_count == 1

    async def test_the_404_page_is_audited_not_skipped(self, db, acme, crawled):
        broken = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/broken").one()
        assert broken.status_code == 404
        assert broken.highest_severity == Severity.CRITICAL

    async def test_website_summary_is_refreshed(self, db, acme, crawled):
        db.refresh(acme)
        assert acme.total_pages >= 4
        assert acme.average_seo_score is not None
        assert acme.critical_issue_count >= 2  # noindex + the 404
        assert acme.last_crawled_at is not None


class TestPageIdentityAcrossCrawls:
    async def test_recrawling_reuses_page_rows(self, db, acme, crawled, monkeypatch):
        """Page identity must survive a re-crawl, or no history is possible."""
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        first_ids = {p.path: p.id for p in db.query(Page).filter(Page.website_id == acme.id)}

        second = create_crawl_run(db, acme)
        await run_pipeline(db, second.id)
        db.expire_all()

        second_ids = {p.path: p.id for p in db.query(Page).filter(Page.website_id == acme.id)}
        assert first_ids == second_ids

    async def test_each_crawl_adds_a_new_audit_row(self, db, acme, crawled, monkeypatch):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        assert db.query(SEOAudit).filter(SEOAudit.page_id == home.id).count() == 1

        second = create_crawl_run(db, acme)
        await run_pipeline(db, second.id)
        db.expire_all()
        assert db.query(SEOAudit).filter(SEOAudit.page_id == home.id).count() == 2


class TestIncrementalCrawl:
    async def test_incremental_run_only_touches_its_targets(self, db, acme, crawled, monkeypatch):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        run = create_crawl_run(
            db, acme, mode=CrawlMode.INCREMENTAL, target_urls=["https://acme.test/about"]
        )
        outcome = await run_pipeline(db, run.id)
        assert outcome.pages_audited == 1

        audits = db.query(SEOAudit).filter(SEOAudit.crawl_run_id == run.id).all()
        assert len(audits) == 1
        assert db.get(Page, audits[0].page_id).path == "/about"

    async def test_incremental_run_does_not_deactivate_untouched_pages(
        self, db, acme, crawled, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        run = create_crawl_run(
            db, acme, mode=CrawlMode.INCREMENTAL, target_urls=["https://acme.test/about"]
        )
        await run_pipeline(db, run.id)
        db.expire_all()
        active = db.query(Page).filter(Page.website_id == acme.id, Page.is_active.is_(True)).count()
        assert active >= 4


class TestPipelineFailureHandling:
    async def test_a_crawl_that_raises_marks_the_run_failed(self, db, acme, monkeypatch):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        run = create_crawl_run(db, acme)

        async def explode(self, on_progress=None):
            raise RuntimeError("network stack melted")

        monkeypatch.setattr("app.services.crawler.orchestrator.Crawler.run", explode)

        with pytest.raises(RuntimeError):
            await run_crawl_pipeline(db, run.id)

        db.expire_all()
        failed = db.get(CrawlRun, run.id)
        assert failed.status == RunStatus.FAILED
        assert "network stack melted" in failed.error

    async def test_unknown_crawl_run_is_rejected(self, db):
        with pytest.raises(ValueError):
            await run_crawl_pipeline(db, 999_999)


class TestCrawlApi:
    def test_starting_a_crawl_returns_a_queued_run(self, client, db, acme, member_user, monkeypatch):
        # Do not actually crawl; this test covers the endpoint contract only.
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")
        response = client.post(
            f"/api/websites/{acme.id}/crawls", json={"mode": "full"},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 202
        assert response.json()["status"] == RunStatus.QUEUED

    def test_a_second_concurrent_crawl_is_refused(self, client, db, acme, member_user, monkeypatch):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")
        headers = auth_headers(member_user)
        client.post(f"/api/websites/{acme.id}/crawls", json={"mode": "full"}, headers=headers)
        second = client.post(
            f"/api/websites/{acme.id}/crawls", json={"mode": "full"}, headers=headers
        )
        assert second.status_code == 409

    def test_incremental_crawl_requires_targets(self, client, acme, member_user, monkeypatch):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")
        response = client.post(
            f"/api/websites/{acme.id}/crawls", json={"mode": "incremental"},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 422

    def test_crawl_progress_can_be_polled(self, client, db, acme, member_user, monkeypatch):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")
        headers = auth_headers(member_user)
        run_id = client.post(
            f"/api/websites/{acme.id}/crawls", json={"mode": "full"}, headers=headers
        ).json()["id"]
        polled = client.get(f"/api/crawls/{run_id}", headers=headers)
        assert polled.status_code == 200
        assert polled.json()["id"] == run_id

    def test_a_crawl_can_be_cancelled(self, client, acme, member_user, monkeypatch):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")
        headers = auth_headers(member_user)
        run_id = client.post(
            f"/api/websites/{acme.id}/crawls", json={"mode": "full"}, headers=headers
        ).json()["id"]
        cancelled = client.post(f"/api/crawls/{run_id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == RunStatus.CANCELLED

    def test_rule_catalogue_is_exposed(self, client, member_user):
        response = client.get("/api/seo/rules", headers=auth_headers(member_user))
        assert response.status_code == 200
        assert len(response.json()) >= 20


class TestPageApi:
    async def test_pages_endpoint_lists_crawled_pages(
        self, client, db, acme, member_user, crawled
    ):
        response = client.get(
            f"/api/websites/{acme.id}/pages", headers=auth_headers(member_user)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total"] >= 4
        assert all("seo_score" in item and "priority_score" in item for item in body["items"])

    async def test_pages_can_be_filtered_by_severity(
        self, client, db, acme, member_user, crawled
    ):
        response = client.get(
            f"/api/websites/{acme.id}/pages?severity=CRITICAL",
            headers=auth_headers(member_user),
        )
        items = response.json()["items"]
        assert items
        assert all(item["highest_severity"] == "CRITICAL" for item in items)

    async def test_pages_can_be_sorted_by_seo_score(self, client, db, acme, member_user, crawled):
        response = client.get(
            f"/api/websites/{acme.id}/pages?sort=seo_score&order=asc",
            headers=auth_headers(member_user),
        )
        scores = [i["seo_score"] for i in response.json()["items"] if i["seo_score"] is not None]
        assert scores == sorted(scores)

    async def test_major_issues_are_summarised_per_row(self, client, db, acme, member_user, crawled):
        response = client.get(
            f"/api/websites/{acme.id}/pages?search=products", headers=auth_headers(member_user)
        )
        item = response.json()["items"][0]
        assert item["top_issues"]

    async def test_page_detail_returns_issues_and_checks(
        self, client, db, acme, member_user, crawled
    ):
        page = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/products").one()
        response = client.get(f"/api/pages/{page.id}", headers=auth_headers(member_user))
        assert response.status_code == 200
        body = response.json()
        assert body["page"]["path"] == "/products"
        assert body["issues"]
        assert body["checks"]
        assert body["metrics"]["users"] == 0  # no integrations connected yet

    async def test_issue_summary_groups_by_rule(self, client, db, acme, member_user, crawled):
        response = client.get(
            f"/api/websites/{acme.id}/issues/summary", headers=auth_headers(member_user)
        )
        body = response.json()
        assert body["by_severity"]
        assert body["by_rule"]
        # Most severe first.
        assert body["by_rule"][0]["severity"] in ("CRITICAL", "HIGH")

    async def test_another_users_page_is_not_readable(self, client, db, acme, crawled):
        from .conftest import make_user

        stranger = make_user(db, email="stranger2@example.com")
        page = db.query(Page).filter(Page.website_id == acme.id).first()
        assert client.get(
            f"/api/pages/{page.id}", headers=auth_headers(stranger)
        ).status_code == 404


class TestDashboardAggregates:
    async def test_portfolio_overview_summarises_every_website(
        self, client, db, acme, member_user, crawled
    ):
        body = client.get("/api/dashboard/overview", headers=auth_headers(member_user)).json()
        assert body["totals"]["websites"] == 1
        assert body["totals"]["pages"] >= 4
        assert body["totals"]["critical_issues"] >= 2
        site = body["websites"][0]
        assert site["id"] == acme.id
        assert set(site["integrations"]) == {"gsc", "ga4", "semrush", "github"}

    async def test_website_overview_reports_distributions(
        self, client, db, acme, member_user, crawled
    ):
        body = client.get(
            f"/api/dashboard/websites/{acme.id}", headers=auth_headers(member_user)
        ).json()
        assert body["summary"]["total_pages"] >= 4
        assert body["distribution"]["seo_category"]
        assert body["distribution"]["status_code"]["2xx"] >= 3
        assert body["distribution"]["status_code"]["4xx"] == 1
        assert body["data_sources"] == ["seo"]

    async def test_each_rule_appears_once_in_the_issue_summaries(
        self, client, db, acme, member_user, crawled
    ):
        """A rule firing at two severities must not be listed twice with partial counts."""
        overview = client.get(
            f"/api/dashboard/websites/{acme.id}", headers=auth_headers(member_user)
        ).json()
        rule_ids = [issue["rule_id"] for issue in overview["top_issues"]]
        assert len(rule_ids) == len(set(rule_ids))

        summary = client.get(
            f"/api/websites/{acme.id}/issues/summary", headers=auth_headers(member_user)
        ).json()
        summary_ids = [rule["rule_id"] for rule in summary["by_rule"]]
        assert len(summary_ids) == len(set(summary_ids))

    async def test_a_stranger_cannot_read_the_website_overview(self, client, db, acme, crawled):
        from .conftest import make_user

        stranger = make_user(db, email="dash-stranger@example.com")
        assert client.get(
            f"/api/dashboard/websites/{acme.id}", headers=auth_headers(stranger)
        ).status_code == 404

    async def test_the_portfolio_only_shows_permitted_websites(self, client, db, acme, crawled):
        from .conftest import make_user

        stranger = make_user(db, email="dash-outsider@example.com")
        body = client.get("/api/dashboard/overview", headers=auth_headers(stranger)).json()
        assert body["totals"]["websites"] == 0

    async def test_trends_endpoint_returns_a_series(self, client, db, acme, member_user, crawled):
        body = client.get(
            f"/api/dashboard/websites/{acme.id}/trends", headers=auth_headers(member_user)
        ).json()
        assert body["days"] == 90
        assert isinstance(body["points"], list)


class TestTimestampsAreUtcAware:
    """Naive timestamps serialise without an offset and browsers read them as local time."""

    async def test_crawl_timestamps_come_back_timezone_aware(self, db, acme, crawled):
        run = db.get(CrawlRun, crawled.crawl_run_id)
        assert run.started_at is not None and run.started_at.tzinfo is not None
        assert run.completed_at.tzinfo is not None
        assert run.created_at.tzinfo is not None

    async def test_page_timestamps_come_back_timezone_aware(self, db, acme, crawled):
        page = db.query(Page).filter(Page.website_id == acme.id).first()
        assert page.last_crawled_at.tzinfo is not None
        assert page.first_seen_at.tzinfo is not None

    async def test_serialised_timestamps_carry_an_offset(
        self, client, db, acme, member_user, crawled
    ):
        body = client.get(
            f"/api/websites/{acme.id}/crawls", headers=auth_headers(member_user)
        ).json()
        completed = body["items"][0]["completed_at"]
        assert completed.endswith("Z") or "+" in completed[10:], completed


class TestCrawlFailureDoesNotDestroyState:
    """A transient outage must not look like a site that lost every page."""

    async def test_a_zero_page_crawl_leaves_pages_active(self, db, acme, crawled, monkeypatch):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        active_before = db.query(Page).filter(
            Page.website_id == acme.id, Page.is_active.is_(True)
        ).count()
        assert active_before >= 4

        # The site is unreachable: every request fails.
        def dead(request):
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(dead)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        run = create_crawl_run(db, acme)
        httpx.AsyncClient.__init__ = patched
        try:
            await run_crawl_pipeline(db, run.id)
        finally:
            httpx.AsyncClient.__init__ = original

        db.expire_all()
        active_after = db.query(Page).filter(
            Page.website_id == acme.id, Page.is_active.is_(True)
        ).count()
        assert active_after == active_before

    async def test_a_successful_recrawl_still_deactivates_removed_pages(
        self, db, acme, crawled, monkeypatch
    ):
        """The deactivation path must still work when the crawl genuinely succeeded."""
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)

        stale_url = "https://acme.test/retired"
        db.add(
            Page(
                website_id=acme.id, url=stale_url, url_hash=url_hash(stale_url),
                path="/retired", is_active=True,
            )
        )
        db.commit()

        run = create_crawl_run(db, acme)
        await run_pipeline(db, run.id)
        db.expire_all()

        retired = db.query(Page).filter(
            Page.website_id == acme.id, Page.path == "/retired"
        ).one()
        assert retired.is_active is False


class TestFailedCrawlDoesNotFabricateMissingData:
    """A crawl that retrieves no document must not blank out a healthy page's signals.

    Overwriting title, headings, word count and links with empty values turns one timeout into a
    page that appears to have lost all its content, and the audit then reports a dozen invented
    "missing" issues for a site that is perfectly fine.
    """

    async def test_a_timeout_preserves_the_previous_content_signals(
        self, db, acme, crawled, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        title_before = home.title
        words_before = home.word_count
        h1_before = home.h1
        assert title_before and words_before > 0

        def dead(request):
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(dead)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        run = create_crawl_run(db, acme)
        httpx.AsyncClient.__init__ = patched
        try:
            await run_crawl_pipeline(db, run.id)
        finally:
            httpx.AsyncClient.__init__ = original

        db.expire_all()
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        assert home.title == title_before
        assert home.word_count == words_before
        assert home.h1 == h1_before

    async def test_a_500_response_preserves_content_but_records_the_status(
        self, db, acme, crawled, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        title_before = home.title
        assert title_before

        def broken(request):
            return httpx.Response(500, text="<html><body>Server Error</body></html>",
                                  headers={"content-type": "text/html"})

        transport = httpx.MockTransport(broken)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        run = create_crawl_run(db, acme)
        httpx.AsyncClient.__init__ = patched
        try:
            await run_crawl_pipeline(db, run.id)
        finally:
            httpx.AsyncClient.__init__ = original

        db.expire_all()
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        # The response fact is current...
        assert home.status_code == 500
        # ...but the content signals are not invented.
        assert home.title == title_before

    async def test_content_captured_at_lags_last_crawled_at_after_a_failure(
        self, db, acme, crawled, monkeypatch
    ):
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        captured_before = home.content_captured_at
        assert captured_before is not None

        def dead(request):
            raise httpx.ConnectError("down")

        transport = httpx.MockTransport(dead)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        run = create_crawl_run(db, acme)
        httpx.AsyncClient.__init__ = patched
        try:
            await run_crawl_pipeline(db, run.id)
        finally:
            httpx.AsyncClient.__init__ = original

        db.expire_all()
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        assert home.content_captured_at == captured_before
        assert home.last_crawled_at >= captured_before
        assert home.crawl_quality != "ok"

    async def test_a_successful_recrawl_does_update_content(self, db, acme, crawled, monkeypatch):
        """The preservation rule must not freeze a page that is genuinely reachable."""
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)

        changed = dict(PAGES)
        changed["/"] = changed["/"].replace(
            "Acme Widgets \u2014 Industrial Fasteners And Fittings", "Acme Widgets — New Title Today"
        )

        def handler(request):
            body = changed.get(request.url.path)
            if body is None:
                return httpx.Response(404, text="<html><body>Not found</body></html>",
                                      headers={"content-type": "text/html"})
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        run = create_crawl_run(db, acme)
        httpx.AsyncClient.__init__ = patched
        try:
            await run_crawl_pipeline(db, run.id)
        finally:
            httpx.AsyncClient.__init__ = original

        db.expire_all()
        home = db.query(Page).filter(Page.website_id == acme.id, Page.path == "/").one()
        assert home.content_captured_at is not None
        assert home.title == "Acme Widgets — New Title Today"


class TestPerRunCrawlLimit:
    def test_a_one_off_limit_does_not_reconfigure_the_website(
        self, client, db, acme, member_user, monkeypatch
    ):
        monkeypatch.setattr("app.api.routes.crawls.dispatch_crawl", lambda *a, **k: "test")
        original_limit = acme.max_pages

        response = client.post(
            f"/api/websites/{acme.id}/crawls",
            json={"mode": "full", "max_pages": 5},
            headers=auth_headers(member_user),
        )
        assert response.status_code == 202

        db.expire_all()
        # The run is capped...
        run = db.get(CrawlRun, response.json()["id"])
        assert run.config_snapshot["max_pages"] == 5
        # ...but the website keeps its configured limit.
        assert db.get(Website, acme.id).max_pages == original_limit
