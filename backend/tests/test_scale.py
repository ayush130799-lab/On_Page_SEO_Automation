"""Scale characteristics.

These are correctness-at-scale tests, not benchmarks: they assert that the pipeline completes on a
large site, that query cost stays flat as the page count grows, and that the crawler's guard rails
actually bind. Generous time budgets keep them stable on slow CI without making them meaningless.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import pytest

from app.models import (
    GA4Metric,
    GSCMetric,
    MemberRole,
    Page,
    PriorityScore,
    SEOAudit,
    SEOIssue,
    Website,
    WebsiteMember,
)
from app.services.crawler import CrawlConfig, Crawler
from app.services.metrics import aggregate_page_metrics
from app.services.pipeline import create_crawl_run, run_crawl_pipeline, upsert_pages
from app.services.priority import compute_priorities, score_website
from app.services.rollup import rollup_website
from app.services.seo import audit_site
from app.utils.url_utils import url_hash, url_path

from .conftest import auth_headers

LARGE = 10_000
MEDIUM = 1_000


@pytest.fixture
def site(db, member_user):
    website = Website(
        name="Big Site", url="https://big.test/", domain="big.test",
        created_by_id=member_user.id, render_mode="never",
    )
    db.add(website)
    db.flush()
    db.add(WebsiteMember(website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER))
    db.commit()
    db.refresh(website)
    return website


def seed_pages(db, site, count: int) -> list[Page]:
    """Insert ``count`` pages with a realistic spread of scores and severities."""
    severities = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"]
    pages = [
        Page(
            website_id=site.id,
            url=f"https://big.test/section-{index % 50}/page-{index}",
            url_hash=url_hash(f"https://big.test/section-{index % 50}/page-{index}"),
            path=url_path(f"https://big.test/section-{index % 50}/page-{index}"),
            is_active=True,
            status_code=200,
            seo_score=40.0 + (index % 60),
            highest_severity=severities[index % len(severities)],
            issue_count=index % 9,
            content_hash=f"hash-{index}",
            word_count=300 + index % 500,
        )
        for index in range(count)
    ]
    db.bulk_save_objects(pages)
    db.commit()
    return db.query(Page).filter(Page.website_id == site.id).all()


class TestAuditEngineAtScale:
    def test_ten_thousand_pages_are_audited(self):
        """The rule engine is the per-page hot path; 24 rules × 10 000 pages must stay tractable."""
        from tests.test_seo_rules import make_page

        pages = [
            make_page(
                url=f"https://big.test/p{index}",
                final_url=f"https://big.test/p{index}",
                content_hash=f"hash-{index % 9000}",  # a realistic sprinkling of duplicates
                title=f"Page {index} — A Reasonably Long Title For Testing",
            )
            for index in range(LARGE)
        ]

        started = time.monotonic()
        results = audit_site(pages)
        elapsed = time.monotonic() - started

        assert len(results) == LARGE
        assert all(0 <= r.seo_score <= 100 for r in results)
        # Comfortably generous; the point is that it is not quadratic.
        assert elapsed < 120, f"auditing {LARGE} pages took {elapsed:.1f}s"

    def test_duplicate_detection_does_not_degrade_quadratically(self):
        """Duplicate grouping is hash-bucketed, so doubling the input must not quadruple the time."""
        from tests.test_seo_rules import make_page

        def run(count: int) -> float:
            pages = [
                make_page(
                    url=f"https://big.test/p{i}", final_url=f"https://big.test/p{i}",
                    content_hash=f"h{i % 100}",
                )
                for i in range(count)
            ]
            started = time.monotonic()
            audit_site(pages)
            return time.monotonic() - started

        small = run(500)
        large = run(2000)
        # 4× the input should cost far less than the 16× a quadratic pass would.
        assert large < small * 10 + 2, f"500 pages: {small:.2f}s, 2000 pages: {large:.2f}s"


class TestPersistenceAtScale:
    def test_pages_are_upserted_in_bulk(self, db, site):
        from app.services.crawler.extractor import ExtractedPage

        extracted = [
            ExtractedPage(
                url=f"https://big.test/p{index}",
                final_url=f"https://big.test/p{index}",
                status_code=200,
                title=f"Page {index}",
                content="content " * 60,
                word_count=120,
                content_hash=f"hash-{index}",
            )
            for index in range(MEDIUM)
        ]

        started = time.monotonic()
        result = upsert_pages(db, site, extracted)
        db.commit()
        elapsed = time.monotonic() - started

        assert len(result) == MEDIUM
        assert db.query(Page).filter(Page.website_id == site.id).count() == MEDIUM
        assert elapsed < 60, f"upserting {MEDIUM} pages took {elapsed:.1f}s"

    def test_re_upserting_reuses_rows_rather_than_duplicating(self, db, site):
        from app.services.crawler.extractor import ExtractedPage

        extracted = [
            ExtractedPage(
                url=f"https://big.test/p{index}", final_url=f"https://big.test/p{index}",
                status_code=200, content_hash=f"hash-{index}",
            )
            for index in range(MEDIUM)
        ]
        upsert_pages(db, site, extracted)
        db.commit()
        first_ids = {p.url: p.id for p in db.query(Page).filter(Page.website_id == site.id)}

        upsert_pages(db, site, extracted)
        db.commit()
        second_ids = {p.url: p.id for p in db.query(Page).filter(Page.website_id == site.id)}

        assert first_ids == second_ids
        assert db.query(Page).filter(Page.website_id == site.id).count() == MEDIUM


class TestScoringAtScale:
    def test_ten_thousand_pages_are_scored(self, db, site):
        seed_pages(db, site, LARGE)

        started = time.monotonic()
        result = compute_priorities(db, site)
        elapsed = time.monotonic() - started

        assert result.pages_scored == LARGE
        assert [p.rank for p in result.priorities[:5]] == [1, 2, 3, 4, 5]
        assert elapsed < 90, f"scoring {LARGE} pages took {elapsed:.1f}s"

    def test_scoring_persists_without_a_query_per_page(self, db, site):
        seed_pages(db, site, MEDIUM)

        started = time.monotonic()
        score_website(db, site)
        elapsed = time.monotonic() - started

        assert db.query(PriorityScore).count() == MEDIUM
        assert elapsed < 60, f"persisting {MEDIUM} scores took {elapsed:.1f}s"

    def test_metric_aggregation_chunks_large_id_lists(self, db, site):
        """SQLite caps bound parameters near 1000; the aggregator must chunk rather than fail."""
        pages = seed_pages(db, site, 2500)
        page_ids = [p.id for p in pages]

        today = date.today()
        db.bulk_save_objects(
            [
                GSCMetric(
                    website_id=site.id, page_id=page_id, date=today - timedelta(days=1),
                    clicks=5, impressions=100, ctr=0.05, position=8.0,
                )
                for page_id in page_ids[:1500]
            ]
        )
        db.commit()

        aggregated = aggregate_page_metrics(db, page_ids, window_days=28)
        assert len(aggregated) == 2500
        assert aggregated[page_ids[0]]["clicks"] == 5
        assert aggregated[page_ids[-1]]["clicks"] == 0


class TestListEndpointsAtScale:
    def test_the_priority_table_pages_a_large_site_quickly(
        self, client, db, site, member_user
    ):
        seed_pages(db, site, MEDIUM)
        score_website(db, site)

        started = time.monotonic()
        response = client.get(
            f"/api/websites/{site.id}/pages?limit=50", headers=auth_headers(member_user)
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == MEDIUM
        assert len(body["items"]) == 50
        # Only the 50 returned rows should be enriched, not all 1000.
        assert elapsed < 10, f"listing took {elapsed:.1f}s"

    def test_deep_pagination_stays_cheap(self, client, db, site, member_user):
        seed_pages(db, site, MEDIUM)
        score_website(db, site)

        started = time.monotonic()
        response = client.get(
            f"/api/websites/{site.id}/pages?limit=50&offset=900",
            headers=auth_headers(member_user),
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert len(response.json()["items"]) == 50
        assert elapsed < 10

    def test_sorting_by_a_metric_column_works_on_a_large_site(
        self, client, db, site, member_user
    ):
        pages = seed_pages(db, site, MEDIUM)
        today = date.today()
        db.bulk_save_objects(
            [
                GA4Metric(
                    website_id=site.id, page_id=page.id, date=today - timedelta(days=1),
                    users=index, sessions=index * 2, conversions=index / 10,
                )
                for index, page in enumerate(pages[:500])
            ]
        )
        db.commit()

        response = client.get(
            f"/api/websites/{site.id}/pages?sort=users&order=desc&limit=20",
            headers=auth_headers(member_user),
        )
        assert response.status_code == 200
        users = [item["users"] for item in response.json()["items"]]
        assert users == sorted(users, reverse=True)

    def test_the_portfolio_overview_stays_flat_across_many_websites(
        self, client, db, member_user
    ):
        for index in range(25):
            website = Website(
                name=f"Site {index}", url=f"https://site{index}.test/",
                domain=f"site{index}.test", total_pages=400,
            )
            db.add(website)
            db.flush()
            db.add(
                WebsiteMember(
                    website_id=website.id, user_id=member_user.id, role=MemberRole.OWNER
                )
            )
        db.commit()

        started = time.monotonic()
        response = client.get("/api/dashboard/overview", headers=auth_headers(member_user))
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert response.json()["totals"]["websites"] == 25
        # The endpoint issues a fixed number of grouped queries regardless of portfolio size.
        assert elapsed < 10, f"portfolio overview took {elapsed:.1f}s"


class TestCrawlerGuardRails:
    """The limits that stop a crawl from running away are only useful if they actually bind."""

    def _transport(self, page_count: int) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /")
            if path.endswith(".xml"):
                locs = "".join(
                    f"<url><loc>https://big.test/p{i}</loc></url>" for i in range(page_count)
                )
                return httpx.Response(
                    200, text=f"<urlset>{locs}</urlset>",
                    headers={"content-type": "application/xml"},
                )
            body = (
                "<html><head><title>A Page With A Perfectly Reasonable Title</title></head>"
                "<body><h1>Heading</h1><p>" + ("text " * 200) + "</p>"
                '<a href="/p1">One</a><a href="/p2">Two</a></body></html>'
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})

        return httpx.MockTransport(handler)

    async def _crawl(self, config: CrawlConfig, page_count: int = 5000):
        crawler = Crawler("https://big.test/", config)
        transport = self._transport(page_count)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        httpx.AsyncClient.__init__ = patched
        try:
            return await crawler.run()
        finally:
            httpx.AsyncClient.__init__ = original

    async def test_the_page_limit_binds(self):
        result = await self._crawl(
            CrawlConfig(
                max_pages=200, concurrency=20, render_enabled=False, allow_local=True,
                crawl_delay=0, rate_limit_per_second=100_000,
            )
        )
        assert result.pages_crawled <= 200
        assert result.truncated is True
        assert "page limit" in (result.truncation_reason or "")

    async def test_the_time_budget_binds(self):
        result = await self._crawl(
            CrawlConfig(
                max_pages=100_000, concurrency=4, render_enabled=False, allow_local=True,
                crawl_delay=0, rate_limit_per_second=100_000, time_budget_seconds=1,
            )
        )
        assert result.truncated is True
        assert "time budget" in (result.truncation_reason or "")

    async def test_a_thousand_page_crawl_completes(self):
        started = time.monotonic()
        result = await self._crawl(
            CrawlConfig(
                max_pages=MEDIUM, concurrency=40, render_enabled=False, allow_local=True,
                crawl_delay=0, rate_limit_per_second=100_000,
            ),
            page_count=MEDIUM,
        )
        elapsed = time.monotonic() - started

        assert result.pages_crawled == MEDIUM
        assert result.pages_failed == 0
        assert elapsed < 120, f"crawling {MEDIUM} pages took {elapsed:.1f}s"


class TestRollupAtScale:
    def test_rollup_caps_the_per_page_series(self, db, site):
        """A per-page daily row for every page would add millions of rows a year."""
        from app.services.rollup import MAX_TRACKED_PAGES_PER_SITE
        from app.models import HistoricalMetric

        seed_pages(db, site, 2000)
        score_website(db, site)

        rollup_website(db, site)

        page_rows = db.query(HistoricalMetric).filter(
            HistoricalMetric.scope == "page"
        ).count()
        website_rows = db.query(HistoricalMetric).filter(
            HistoricalMetric.scope == "website"
        ).count()

        assert page_rows == MAX_TRACKED_PAGES_PER_SITE
        assert website_rows == 1

    def test_rolling_up_twice_overwrites_rather_than_duplicating(self, db, site):
        from app.models import HistoricalMetric

        seed_pages(db, site, 20)
        score_website(db, site)

        rollup_website(db, site)
        first = db.query(HistoricalMetric).count()
        rollup_website(db, site)

        assert db.query(HistoricalMetric).count() == first


class TestFullPipelineAtScale:
    async def test_a_large_crawl_persists_end_to_end(self, db, site, monkeypatch):
        """Crawl → audit → persist → score, on a site big enough to expose N+1 behaviour."""
        monkeypatch.setattr("app.config.settings.allow_local_crawl", True)
        monkeypatch.setattr("app.config.settings.render_enabled", False)
        site.max_pages = 300
        db.commit()

        guard = TestCrawlerGuardRails()
        transport = guard._transport(300)
        original = httpx.AsyncClient.__init__

        def patched(self, *args, **kwargs):
            kwargs["transport"] = transport
            return original(self, *args, **kwargs)

        run = create_crawl_run(db, site)
        httpx.AsyncClient.__init__ = patched
        try:
            started = time.monotonic()
            outcome = await run_crawl_pipeline(db, run.id)
            elapsed = time.monotonic() - started
        finally:
            httpx.AsyncClient.__init__ = original

        assert outcome.pages_audited >= 200
        assert db.query(SEOAudit).filter(SEOAudit.crawl_run_id == run.id).count() == (
            outcome.pages_audited
        )
        assert db.query(SEOIssue).count() > 0
        assert elapsed < 180, f"the 300-page pipeline took {elapsed:.1f}s"

        score_website(db, site)
        db.expire_all()
        assert (
            db.query(Page).filter(
                Page.website_id == site.id, Page.priority_score.isnot(None)
            ).count()
            == outcome.pages_audited
        )
