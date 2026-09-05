"""Crawler debugging mode: fetch one URL live and explain every extracted value.

    GET /api/validate/page?url=https://example.com/blog

This is the tool for answering "why does our crawler say X when another tool says Y?". For a
single URL it reports the complete response facts (final URL, redirect chain, every response
header, timing, byte count), whether JavaScript rendering was applied and why, every extracted
signal, and — crucially — the *provenance* of each value: the selector used, how many nodes it
matched, the raw attribute text before normalisation, and the decision taken.

Two properties matter for this endpoint to be trustworthy:

* It runs the **same** fetch, render, extraction and rule code the crawler runs. It re-derives
  nothing. An earlier version re-implemented the robots check with substring matching and could
  therefore report a different verdict than the crawl that produced the stored data, which is
  precisely the class of bug this endpoint exists to find.
* A failed fetch is reported as a failed fetch. It never becomes a page full of "missing" SEO
  findings — ``is_usable`` is surfaced explicitly, and the rule engine skips checks it cannot
  honestly evaluate.

``?render=`` accepts ``auto`` (default — render only when the static HTML looks client-rendered,
matching crawl behaviour), ``always``, or ``never``. ``?html=true`` additionally returns the raw
and rendered HTML so the DOM can be diffed by hand.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from ...config import settings
from ...core.deps import CurrentUser
from ...services.crawler.extractor import extract_page
from ...services.crawler.fetcher import HostRateLimiter, fetch_url
from ...services.crawler.renderer import PlaywrightRenderer, needs_rendering
from ...services.crawler.robots import parse_robots
from ...services.seo.engine import audit_page
from ...services.seo.robots_directives import describe, resolve
from ...utils.url_utils import domain_of, normalize_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/validate", tags=["validate"])

MAX_HTML_RETURNED = 400_000


@router.get("/page")
async def validate_page_extraction(
    _: CurrentUser,
    url: str = Query(..., description="Full URL to fetch and inspect, e.g. https://example.com/about"),
    render: str = Query("auto", pattern="^(auto|always|never)$"),
    html: bool = Query(False, description="Include raw and rendered HTML in the response"),
    check_robots: bool = Query(True, description="Also fetch robots.txt and report the verdict"),
) -> dict[str, Any]:
    """Live-fetch one URL and return every signal with the provenance of each value."""
    try:
        requested_url = normalize_url(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL format.")

    base_domain = domain_of(requested_url)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout),
        follow_redirects=True,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ) as client:
        limiter = HostRateLimiter(1.0)
        result = await fetch_url(
            client, requested_url, limiter=limiter, max_retries=1,
            timeout=settings.request_timeout,
        )

        robots_verdict: dict[str, Any] | None = None
        if check_robots:
            robots_verdict = await _robots_verdict(client, requested_url)

    raw_html = result.html or ""
    final_html = raw_html
    rendered = False
    render_error: str | None = None
    render_reason: str

    if render == "never":
        render_reason = "Rendering disabled by request (render=never)."
    elif not raw_html:
        render_reason = "Nothing to render — the fetch returned no HTML body."
    else:
        # The query parameter is the render mode, so this is the crawler's own decision function
        # applied to the mode the caller asked about.
        wanted = needs_rendering(raw_html, render_mode=render)
        if not wanted:
            render_reason = (
                "Static HTML already contains enough text; the crawler would not render this page."
            )
        else:
            render_reason = (
                "Requested explicitly (render=always)."
                if render == "always"
                else "Static HTML looks client-rendered, so the crawler would render this page."
            )
            renderer = PlaywrightRenderer()
            try:
                rendered_html = await renderer.render(
                    result.final_url or requested_url, settings.user_agent
                )
                if rendered_html:
                    final_html = rendered_html
                    rendered = True
                else:
                    render_error = "Renderer returned no HTML; values come from static HTML."
            except Exception as exc:
                render_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Debug render failed for %s: %s", requested_url, exc)
            finally:
                await renderer.close()

    # Extraction and auditing run through the crawler's own functions, with the crawler's own
    # inputs, so this endpoint cannot disagree with a real crawl of the same URL.
    page = extract_page(
        result.final_url or requested_url,
        final_html,
        base_domain,
        result.status_code,
        headers=result.headers,
    )
    page.final_url = result.final_url or requested_url
    page.redirect_chain = result.redirect_chain
    page.was_rendered = rendered
    page.render_error = render_error
    page.response_time_ms = result.elapsed_ms
    page.content_bytes = result.content_bytes
    page.crawl_error = result.error
    if result.error or not raw_html:
        page.crawl_quality = "failed" if result.status_code == 0 else "partial"
    elif render_error:
        page.crawl_quality = "render_failed"

    audit = audit_page(page)
    directives = resolve(page.meta_robots, page.x_robots_tag)

    response: dict[str, Any] = {
        "requested_url": requested_url,
        "final_url": page.final_url,
        "fetch": {
            "status_code": result.status_code,
            "ok": result.ok,
            "redirect_chain": result.redirect_chain,
            "redirect_hops": len(result.redirect_chain),
            "content_type": result.content_type,
            "charset": page.charset,
            "elapsed_ms": result.elapsed_ms,
            "content_bytes": result.content_bytes,
            "attempts": result.attempts,
            "error": result.error,
            # Every header, not a curated subset — an unexpected header is often the answer.
            "response_headers": result.headers,
        },
        "rendering": {
            "was_rendered": rendered,
            "decision": render_reason,
            "mode_requested": render,
            "render_enabled_globally": settings.render_enabled,
            "min_text_length_threshold": settings.render_min_text_length,
            "error": render_error,
            "static_html_bytes": len(raw_html.encode("utf-8", "ignore")),
            "final_html_bytes": len(final_html.encode("utf-8", "ignore")),
        },
        "usability": {
            "is_usable": page.is_usable,
            "crawl_quality": page.crawl_quality,
            "extraction_errors": page.extraction_errors,
            "note": (
                "Not usable — SEO checks other than the HTTP status rule were skipped rather than "
                "reported as failures against a document we never retrieved."
                if not page.is_usable
                else "Usable: every registered rule was evaluated."
            ),
        },
        "indexability": {
            "meta_robots": page.meta_robots,
            "meta_robots_count": page.meta_robots_count,
            "x_robots_tag": page.x_robots_tag,
            "resolved_directives": directives.as_evidence(),
            "summary": describe(directives),
            "indexable": directives.indexable,
            "robots_txt": robots_verdict,
        },
        "canonical": {
            "declared_raw": page.canonical_raw,
            "resolved": page.canonical_url,
            "count": page.canonical_count,
            "status": page.canonical_status,
            "note": (
                "Google's chosen canonical cannot be observed by a crawler; this reports only what "
                "the page declares."
            ),
        },
        "signals": {
            "title": page.title,
            "title_length": len(page.title or ""),
            "title_count": page.title_count,
            "meta_description": page.meta_description,
            "meta_description_length": len(page.meta_description or ""),
            "meta_description_count": page.meta_description_count,
            "lang": page.lang,
            "hreflang": page.hreflang,
            "has_viewport": page.has_viewport,
            "h1": page.h1,
            "headings": {f"h{n}": getattr(page, f"h{n}_count") for n in range(1, 7)},
            "empty_heading_count": page.empty_heading_count,
            "heading_outline": page.headings[:50],
            "word_count": page.word_count,
            "raw_word_count": page.raw_word_count,
            "visible_word_count": page.visible_word_count,
            "main_content_word_count": page.main_content_word_count,
            "content_scope": page.content_scope,
            "content_preview": page.content[:500] or None,
            "image_count": page.image_count,
            "missing_alt_count": page.missing_alt_count,
            "empty_alt_count": page.empty_alt_count,
            "images_without_dimensions": page.images_without_dimensions,
            "tracking_pixel_count": page.tracking_pixel_count,
            "internal_link_count": page.internal_link_count,
            "external_link_count": page.external_link_count,
            "nofollow_link_count": page.nofollow_link_count,
            "sponsored_link_count": page.sponsored_link_count,
            "ugc_link_count": page.ugc_link_count,
            "non_http_link_count": page.non_http_link_count,
            "pagination_next": page.pagination_next,
            "pagination_prev": page.pagination_prev,
            "has_structured_data": page.has_structured_data,
            "structured_data_types": page.structured_data_types,
            "structured_data_formats": page.structured_data_formats,
            "structured_data_invalid": page.structured_data_invalid,
            "json_ld_error": page.json_ld_error,
            "has_open_graph": page.has_open_graph,
            "has_twitter_card": page.has_twitter_card,
        },
        # The heart of this endpoint: for every headline value, what produced it.
        "provenance": page.provenance,
        "samples": {
            "images": [
                {
                    "src": img.src,
                    "alt": img.alt,
                    "alt_state": (
                        "missing" if img.alt is None
                        else "empty" if img.alt.strip() == ""
                        else "present"
                    ),
                    "has_dimensions": bool(img.width and img.height),
                    "is_tracking_pixel": img.is_tracking_pixel,
                }
                for img in page.images[:25]
            ],
            "links": [
                {
                    "url": link.url,
                    "anchor_text": link.anchor_text,
                    "scope": "internal" if link.is_internal else "external",
                    "rel": link.rel,
                }
                for link in page.links[:50]
            ],
        },
        "audit": {
            "seo_score": audit.seo_score,
            "category": audit.category,
            "highest_severity": audit.highest_severity,
            "priority_band": audit.priority_band,
            "checks": [
                {
                    "rule_id": r.rule_id,
                    "check_type": r.check_type,
                    "status": r.status,
                    "score": r.score,
                    "weight": audit.weights.get(r.check_type, 0.0),
                    "severity": r.severity,
                    "details": r.details,
                    "evidence": r.evidence,
                }
                for r in audit.results
            ],
        },
    }

    if html:
        response["html"] = {
            "static": raw_html[:MAX_HTML_RETURNED],
            "rendered": final_html[:MAX_HTML_RETURNED] if rendered else None,
            "truncated": len(final_html) > MAX_HTML_RETURNED,
        }

    return response


async def _robots_verdict(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """Fetch robots.txt for this URL's host and report whether our agent may crawl it."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = await client.get(robots_url, timeout=10.0)
    except Exception as exc:
        # An unreachable robots.txt is not a disallow — say so rather than guessing.
        return {
            "robots_txt_url": robots_url,
            "fetched": False,
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "allowed": True,
            "note": "robots.txt could not be fetched; crawling is permitted by default.",
        }

    if response.status_code != 200:
        return {
            "robots_txt_url": robots_url,
            "fetched": False,
            "status_code": response.status_code,
            "allowed": True,
            "note": f"robots.txt returned HTTP {response.status_code}; crawling is permitted.",
        }

    rules = parse_robots(response.text, settings.user_agent)
    return {
        "robots_txt_url": robots_url,
        "fetched": True,
        "allowed": rules.is_allowed(url),
        "disallow_rules": rules.disallow[:50],
        "allow_rules": rules.allow[:50],
        "crawl_delay": rules.crawl_delay,
        "sitemaps": rules.sitemaps,
    }
