"""Crawl orchestration: discovery, a bounded worker pool over a URL frontier, and page extraction.

Failure isolation is the central guarantee — a timeout, a 5xx or an unexpected exception on one URL
is recorded against that page and never aborts the run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx

from ...config import settings
from ...utils.url_utils import (
    domain_of,
    is_probably_page,
    is_safe_url,
    is_same_domain,
    matches_any_pattern,
    normalize_url,
)
from .extractor import ExtractedPage, empty_page, extract_page
from .fetcher import HostRateLimiter, fetch_url
from .renderer import PlaywrightRenderer, needs_rendering
from .robots import RobotsRules, fetch_robots
from .sitemap import collect_sitemap_urls, default_sitemap_candidates

logger = logging.getLogger(__name__)

ProgressCallback = Callable[["CrawlProgress"], Awaitable[None] | None]


@dataclass
class CrawlConfig:
    """Effective crawl settings — website overrides layered over global defaults."""

    max_pages: int = field(default_factory=lambda: settings.max_pages)
    concurrency: int = field(default_factory=lambda: settings.concurrent_workers)
    request_timeout: float = field(default_factory=lambda: float(settings.request_timeout))
    max_retries: int = field(default_factory=lambda: settings.max_retries)
    crawl_delay: float = field(default_factory=lambda: settings.crawl_delay)
    rate_limit_per_second: float = field(default_factory=lambda: settings.rate_limit_per_second)
    respect_robots_txt: bool = field(default_factory=lambda: settings.respect_robots_txt)
    render_mode: str = "auto"
    render_enabled: bool = field(default_factory=lambda: settings.render_enabled)
    render_max_pages: int = field(default_factory=lambda: settings.render_max_pages)
    time_budget_seconds: int = field(default_factory=lambda: settings.crawl_time_budget_seconds)
    allow_local: bool = field(default_factory=lambda: settings.allow_local_crawl)
    user_agent: str = field(default_factory=lambda: settings.user_agent)
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    #: When set, only these URLs are fetched (incremental re-audit after a code change).
    target_urls: list[str] | None = None
    follow_links: bool = True

    @classmethod
    def for_website(cls, website, **overrides) -> "CrawlConfig":
        """Build a config from a Website row, ignoring unset columns."""
        config = cls(
            max_pages=website.max_pages or settings.max_pages,
            crawl_delay=(
                website.crawl_delay if website.crawl_delay is not None else settings.crawl_delay
            ),
            respect_robots_txt=website.respect_robots_txt,
            render_mode=website.render_mode or "auto",
            include_patterns=website.include_patterns,
            exclude_patterns=website.exclude_patterns,
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(config, key, value)
        return config


@dataclass
class CrawlProgress:
    urls_discovered: int = 0
    pages_queued: int = 0
    pages_crawled: int = 0
    pages_rendered: int = 0
    pages_failed: int = 0
    stage: str = "discovering"


@dataclass
class CrawlResult:
    pages: list[ExtractedPage] = field(default_factory=list)
    urls_discovered: int = 0
    pages_crawled: int = 0
    pages_rendered: int = 0
    pages_failed: int = 0
    duration_seconds: float = 0.0
    robots_blocked: int = 0
    truncated: bool = False
    truncation_reason: str | None = None


class Crawler:
    """One crawl of one website."""

    def __init__(self, start_url: str, config: CrawlConfig | None = None):
        self.start_url = normalize_url(start_url)
        self.config = config or CrawlConfig()
        self.base_domain = domain_of(self.start_url)
        parsed = urlparse(self.start_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

        self.visited: set[str] = set()
        self.enqueued: set[str] = set()
        self.pages: list[ExtractedPage] = []
        self.status_by_url: dict[str, int] = {}
        self.robots = RobotsRules(fetched=False)
        self.robots_blocked = 0
        self.truncated = False
        self.truncation_reason: str | None = None

        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._renderer: PlaywrightRenderer | None = None
        self._deadline: float = 0.0

    # ── URL admission ───────────────────────────────────────────────────────

    def _should_crawl(self, url: str) -> bool:
        if not is_same_domain(url, self.base_domain):
            return False
        if not is_probably_page(url):
            return False
        if not is_safe_url(url, allow_local=self.config.allow_local):
            return False
        if self.config.exclude_patterns and matches_any_pattern(url, self.config.exclude_patterns):
            return False
        if self.config.include_patterns and not matches_any_pattern(
            url, self.config.include_patterns
        ):
            return False
        if self.config.respect_robots_txt and not self.robots.is_allowed(url):
            self.robots_blocked += 1
            return False
        return True

    def _enqueue(self, url: str) -> bool:
        """Add a URL to the frontier. Caller must hold the lock when running concurrently."""
        if url in self.enqueued:
            return False
        if len(self.enqueued) >= self.config.max_pages:
            # The site has more crawlable URLs than the limit allows. Record it here rather than
            # only in the worker: when discovery alone exceeds the cap the workers never reach
            # their own check, and the run would otherwise report a complete crawl of a
            # partially-crawled site.
            self.truncated = True
            self.truncation_reason = (
                f"Reached the {self.config.max_pages}-page limit; "
                "the site has more crawlable URLs."
            )
            return False
        if not self._should_crawl(url):
            return False
        self.enqueued.add(url)
        self._queue.put_nowait(url)
        return True

    # ── Discovery ───────────────────────────────────────────────────────────

    async def _discover(self, client: httpx.AsyncClient) -> None:
        """Seed the frontier: robots.txt → sitemaps → the start URL itself."""
        if self.config.target_urls:
            for url in self.config.target_urls:
                self._enqueue(normalize_url(url))
            logger.info("Incremental crawl seeded with %d target URL(s).", len(self.enqueued))
            return

        self.robots = await fetch_robots(client, self.origin, self.config.user_agent)
        if self.robots.crawl_delay and self.robots.crawl_delay > self.config.crawl_delay:
            # A site asking for slower crawling is honoured.
            self.config.crawl_delay = min(self.robots.crawl_delay, 10.0)

        self._enqueue(self.start_url)

        sitemap_urls = list(self.robots.sitemaps) or default_sitemap_candidates(self.start_url)
        discovered = await collect_sitemap_urls(
            client,
            sitemap_urls,
            self.base_domain,
            # One over the cap, so that a site with more URLs than the limit is detectable
            # rather than looking like an exactly-full crawl.
            self.config.max_pages + 1,
            timeout=self.config.request_timeout,
        )
        for url in discovered:
            self._enqueue(url)

        logger.info(
            "Discovery seeded %d URL(s) for %s (robots.txt %s).",
            len(self.enqueued),
            self.base_domain,
            "found" if self.robots.fetched else "absent",
        )

    # ── Worker ──────────────────────────────────────────────────────────────

    async def _process(self, url: str, client: httpx.AsyncClient, limiter: HostRateLimiter) -> None:
        result = await fetch_url(
            client,
            url,
            limiter=limiter,
            max_retries=self.config.max_retries,
            timeout=self.config.request_timeout,
        )

        async with self._lock:
            self.status_by_url[url] = result.status_code
            self.status_by_url[result.final_url] = result.status_code

        if result.status_code == 0 or (not result.html and not result.ok):
            page = empty_page(
                result.final_url,
                result.status_code or 0,
                result.error or f"No HTML body (HTTP {result.status_code}).",
            )
            page.redirect_chain = result.redirect_chain
            page.response_time_ms = result.elapsed_ms
            async with self._lock:
                self.pages.append(page)
            return

        html = result.html
        rendered = False
        if (
            self.config.render_enabled
            and self._renderer is not None
            and self._renderer.rendered_count < self.config.render_max_pages
            and needs_rendering(html, render_mode=self.config.render_mode)
        ):
            rendered_html = await self._renderer.render(result.final_url, self.config.user_agent)
            if rendered_html:
                html = rendered_html
                rendered = True

        page = extract_page(result.final_url, html, self.base_domain, result.status_code)
        page.final_url = result.final_url
        page.redirect_chain = result.redirect_chain
        page.was_rendered = rendered
        page.response_time_ms = result.elapsed_ms
        page.content_bytes = result.content_bytes

        async with self._lock:
            self.pages.append(page)
            if self.config.follow_links and not self.config.target_urls:
                is_non_canonical = False
                if page.canonical_url:
                    norm_canonical = normalize_url(page.canonical_url)
                    norm_own = normalize_url(page.final_url or page.url)
                    if norm_canonical != norm_own:
                        is_non_canonical = True
                        self._enqueue(norm_canonical)

                # Only expand internal links from canonical pages to avoid exponential parameter link loops
                if not is_non_canonical:
                    for link in page.internal_links:
                        self._enqueue(link)
                for hop in page.redirect_chain:
                    self._enqueue(hop)

    async def _worker(
        self,
        client: httpx.AsyncClient,
        limiter: HostRateLimiter,
        on_progress: ProgressCallback | None,
    ) -> None:
        while True:
            url = await self._queue.get()
            try:
                async with self._lock:
                    if url in self.visited:
                        continue
                    if len(self.visited) >= self.config.max_pages:
                        self.truncated = True
                        self.truncation_reason = f"Reached the {self.config.max_pages}-page limit."
                        continue
                    if time.monotonic() > self._deadline:
                        self.truncated = True
                        self.truncation_reason = "Crawl time budget exhausted."
                        continue
                    self.visited.add(url)

                try:
                    await self._process(url, client, limiter)
                except Exception as exc:
                    # Failure isolation: this URL is recorded as failed, the crawl continues.
                    logger.exception("Isolated failure while crawling %s: %s", url, exc)
                    async with self._lock:
                        self.pages.append(
                            empty_page(url, 0, f"{type(exc).__name__}: {exc}")
                        )

                await self._report(on_progress)
            finally:
                self._queue.task_done()
                if self.config.crawl_delay > 0:
                    await asyncio.sleep(self.config.crawl_delay)

    async def _report(self, on_progress: ProgressCallback | None) -> None:
        if on_progress is None:
            return
        async with self._lock:
            progress = CrawlProgress(
                urls_discovered=len(self.enqueued),
                pages_queued=self._queue.qsize(),
                pages_crawled=len(self.visited),
                pages_rendered=self._renderer.rendered_count if self._renderer else 0,
                pages_failed=sum(1 for p in self.pages if p.crawl_error),
                stage="crawling",
            )
        try:
            outcome = on_progress(progress)
            if asyncio.iscoroutine(outcome):
                await outcome
        except Exception as exc:  # progress reporting must never break a crawl
            logger.debug("Progress callback failed: %s", exc)

    # ── Entry point ─────────────────────────────────────────────────────────

    async def run(self, on_progress: ProgressCallback | None = None) -> CrawlResult:
        if not is_safe_url(self.start_url, allow_local=self.config.allow_local):
            raise ValueError(
                f"{self.start_url} is not a permitted crawl target "
                "(non-HTTP scheme, or it resolves to a private address)."
            )

        started = time.monotonic()
        self._deadline = started + self.config.time_budget_seconds

        if self.config.render_enabled and self.config.render_mode != "never":
            self._renderer = PlaywrightRenderer()

        limits = httpx.Limits(
            max_connections=max(10, self.config.concurrency * 2),
            max_keepalive_connections=max(5, self.config.concurrency),
        )
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.request_timeout),
            follow_redirects=True,
            limits=limits,
            headers=headers,
        ) as client:
            await self._discover(client)

            limiter = HostRateLimiter(self.config.rate_limit_per_second)
            worker_count = max(1, min(self.config.concurrency, max(1, len(self.enqueued))))
            workers = [
                asyncio.create_task(self._worker(client, limiter, on_progress))
                for _ in range(worker_count)
            ]
            try:
                await self._queue.join()
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

        if self._renderer is not None:
            await self._renderer.close()

        self._finalise_links()

        return CrawlResult(
            pages=self.pages,
            urls_discovered=len(self.enqueued),
            pages_crawled=len(self.visited),
            pages_rendered=self._renderer.rendered_count if self._renderer else 0,
            pages_failed=sum(1 for p in self.pages if p.crawl_error),
            duration_seconds=round(time.monotonic() - started, 2),
            robots_blocked=self.robots_blocked,
            truncated=self.truncated,
            truncation_reason=self.truncation_reason,
        )

    def _finalise_links(self) -> None:
        """Resolve broken-link and inbound-link counts from what the crawl already observed.

        Doing this from the status map costs zero extra requests — every internal link was either
        crawled or is known to be outside the frontier.
        """
        inbound: dict[str, int] = {}
        for page in self.pages:
            page.broken_link_count = sum(
                1 for link in page.internal_links if self.status_by_url.get(link, 200) >= 400
            )
            for link in page.internal_links:
                inbound[link] = inbound.get(link, 0) + 1

        for page in self.pages:
            keys = {page.url}
            if page.final_url:
                keys.add(page.final_url)
            page.inbound_internal_links = max((inbound.get(key, 0) for key in keys), default=0)


async def crawl_website(
    start_url: str,
    config: CrawlConfig | None = None,
    on_progress: ProgressCallback | None = None,
) -> CrawlResult:
    """Convenience wrapper around :class:`Crawler`."""
    return await Crawler(start_url, config).run(on_progress)
