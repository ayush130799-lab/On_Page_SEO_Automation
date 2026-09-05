"""SerpApi client — the licensed intermediary for live Google search results.

SerpApi (serpapi.com) is used instead of scraping Google's own SERP HTML: the roadmap itself
warns against building a system "dependent on scraping consumer [...] interfaces" and says to
"use official APIs or permitted data sources wherever possible" (§5.2) — directly scraping
Google Search results pages violates Google's Terms of Service, whereas a licensed SERP API
returns the same data (rankings, snippets, People Also Ask, related searches) through a paid,
permitted channel.

This module only talks to SerpApi. Fetching the competitors' own pages afterwards — a plain HTTP
GET of a public webpage — is a separate, unrelated concern handled in
:mod:`app.services.serp.competitor_analyzer`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...config import settings

logger = logging.getLogger(__name__)


class SerpApiError(Exception):
    """A SerpApi call failed in a way the caller needs to know about."""


@dataclass(slots=True)
class OrganicResult:
    position: int
    url: str
    title: str | None
    snippet: str | None


@dataclass(slots=True)
class SerpResult:
    keyword: str
    organic_results: list[OrganicResult] = field(default_factory=list)
    #: [{"question": ..., "snippet": ...}, ...] — Google's "People Also Ask" box.
    paa_questions: list[dict[str, Any]] = field(default_factory=list)
    related_searches: list[str] = field(default_factory=list)


def is_configured() -> bool:
    return bool(settings.serpapi_key)


async def search(
    keyword: str,
    *,
    num_results: int | None = None,
    location: str | None = None,
    language: str | None = None,
) -> SerpResult:
    """Run one Google search through SerpApi and return organic results + PAA + related searches.

    Raises :class:`SerpApiError` on any failure — an unconfigured key, a network error, a
    non-200 response, or a payload SerpApi itself flags as an error (e.g. an exhausted quota).
    Callers decide how to degrade; this function never returns a partial/guessed result.
    """
    if not settings.serpapi_key:
        raise SerpApiError(
            "SERPAPI_KEY is not configured. Set it in the environment to enable competitor "
            "analysis; this feature is inactive without it."
        )

    params = {
        "engine": "google",
        "q": keyword,
        "api_key": settings.serpapi_key,
        "google_domain": settings.serpapi_google_domain,
        "gl": location or settings.serpapi_gl,
        "hl": language or settings.serpapi_hl,
        "num": str(num_results or max(10, settings.competitor_top_n)),
    }

    try:
        async with httpx.AsyncClient(timeout=settings.serpapi_timeout) as client:
            response = await client.get(settings.serpapi_api_base, params=params)
    except httpx.HTTPError as exc:
        raise SerpApiError(f"SerpApi request failed: {exc}") from exc

    if response.status_code != 200:
        raise SerpApiError(
            f"SerpApi returned HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise SerpApiError(f"SerpApi returned a non-JSON response: {exc}") from exc

    if data.get("error"):
        raise SerpApiError(f"SerpApi reported an error: {data['error']}")

    organic: list[OrganicResult] = []
    for item in data.get("organic_results") or []:
        link = item.get("link")
        if not link:
            continue
        organic.append(OrganicResult(
            position=item.get("position") or len(organic) + 1,
            url=link,
            title=item.get("title"),
            snippet=item.get("snippet"),
        ))

    paa: list[dict[str, Any]] = [
        {"question": q.get("question"), "snippet": q.get("snippet")}
        for q in (data.get("related_questions") or [])
        if q.get("question")
    ]

    related = [
        r.get("query") for r in (data.get("related_searches") or []) if r.get("query")
    ]

    logger.info(
        "SerpApi search for %r returned %d organic result(s), %d PAA question(s).",
        keyword, len(organic), len(paa),
    )

    return SerpResult(
        keyword=keyword, organic_results=organic, paa_questions=paa, related_searches=related,
    )
