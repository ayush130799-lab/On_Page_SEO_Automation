"""Fetch and measure the pages currently ranking for a keyword.

Reuses the crawler's own fetcher and extractor (:mod:`app.services.crawler.fetcher`,
:mod:`app.services.crawler.extractor`) rather than writing a second HTML-measurement path — the
whole point of this feature is that "your page has 400 words, the top-5 average is 1,650" is a
fair comparison only when both sides are counted the same way. Those modules already went through
a full accuracy audit elsewhere in this project; reusing them here means competitor numbers
inherit that same accuracy rather than a hastily-written second implementation.

Fetching a competitor's own public page is a plain HTTP GET, unrelated to the Google-ToS concern
that governs how *search results* are obtained (see ``client.py``). It still fails ungracefully
often in practice — bot-detection, paywalls, geofencing — so every failure is recorded rather than
silently dropped, and a partial result set (3 of 5 fetched) is still a usable comparison.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ...config import settings
from ...utils.url_utils import domain_of
from ..crawler.extractor import ExtractedPage, extract_page
from ..crawler.fetcher import fetch_url
from .client import OrganicResult

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": settings.user_agent,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class CompetitorFetch:
    """One competitor URL's fetch + extraction outcome, ready to persist as a CompetitorResult."""

    __slots__ = (
        "position", "url", "domain", "title", "snippet",
        "fetch_status", "fetch_error", "page",
    )

    def __init__(self, result: OrganicResult):
        self.position = result.position
        self.url = result.url
        self.domain = domain_of(result.url)
        self.title = result.title
        self.snippet = result.snippet
        self.fetch_status = "ok"
        self.fetch_error: str | None = None
        self.page: ExtractedPage | None = None


async def _fetch_one(
    client: httpx.AsyncClient, result: OrganicResult, semaphore: asyncio.Semaphore
) -> CompetitorFetch:
    outcome = CompetitorFetch(result)
    async with semaphore:
        fetched = await fetch_url(
            client, result.url, max_retries=1, timeout=settings.competitor_fetch_timeout,
        )

    if fetched.error:
        outcome.fetch_status = "timeout" if "timeout" in fetched.error.lower() else "failed"
        outcome.fetch_error = fetched.error
        return outcome

    if fetched.status_code in (403, 429):
        outcome.fetch_status = "blocked"
        outcome.fetch_error = f"HTTP {fetched.status_code} — the site likely blocks automated fetches."
        return outcome

    if fetched.status_code >= 400 or not fetched.html:
        outcome.fetch_status = "failed"
        outcome.fetch_error = f"HTTP {fetched.status_code}, no usable HTML body."
        return outcome

    outcome.page = extract_page(
        fetched.final_url, fetched.html, outcome.domain, fetched.status_code,
        headers=fetched.headers,
    )
    return outcome


async def fetch_competitors(
    organic_results: list[OrganicResult], *, top_n: int | None = None,
) -> list[CompetitorFetch]:
    """Fetch and measure the top N organic results concurrently.

    Order is preserved (by SERP position) regardless of which fetches finish first or fail —
    the caller compares "position 1, 2, 3..." meaningfully rather than "whichever responded
    fastest."
    """
    limit = top_n or settings.competitor_top_n
    targets = organic_results[:limit]
    if not targets:
        return []

    semaphore = asyncio.Semaphore(max(1, settings.competitor_fetch_concurrency))
    async with httpx.AsyncClient(
        follow_redirects=True, headers=_HEADERS,
        limits=httpx.Limits(max_connections=settings.competitor_fetch_concurrency),
    ) as client:
        outcomes = await asyncio.gather(
            *(_fetch_one(client, r, semaphore) for r in targets)
        )

    ok = sum(1 for o in outcomes if o.fetch_status == "ok")
    logger.info(
        "Fetched %d/%d competitor pages successfully.", ok, len(outcomes),
    )
    return list(outcomes)
