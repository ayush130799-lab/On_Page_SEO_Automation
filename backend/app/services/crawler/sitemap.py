"""Sitemap discovery.

Handles the three shapes found in the wild: a plain ``<urlset>``, a ``<sitemapindex>`` pointing at
more sitemaps (recursively, with a depth cap), and gzip-compressed ``.xml.gz`` variants.

Parsing uses ``lxml`` when the document is well-formed and falls back to a regex sweep otherwise —
a surprising number of production sitemaps are not valid XML, and refusing to read them would lose
the single best source of URLs a site offers.
"""

from __future__ import annotations

import gzip
import logging
import re
from urllib.parse import urlparse

import httpx

from ...utils.url_utils import is_probably_page, is_same_domain, normalize_url

logger = logging.getLogger(__name__)

MAX_SITEMAP_DEPTH = 4
MAX_SITEMAPS_PER_INDEX = 50

_LOC_RE = re.compile(r"(?<![a-zA-Z0-9_:-])<loc>\s*(https?://[^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAP_BLOCK_RE = re.compile(r"<sitemap[\s>].*?</sitemap>", re.IGNORECASE | re.DOTALL)


def _decode(response: httpx.Response) -> str:
    """Return sitemap text, transparently decompressing a ``.gz`` payload."""
    content = response.content
    if content[:2] == b"\x1f\x8b":  # gzip magic number
        try:
            content = gzip.decompress(content)
        except OSError:
            return response.text
        return content.decode("utf-8", errors="replace")
    return response.text


def _extract_locs(xml: str) -> tuple[list[str], list[str]]:
    """Split ``<loc>`` values into (nested sitemap URLs, page URLs)."""
    sitemap_locs: list[str] = []
    for block in _SITEMAP_BLOCK_RE.findall(xml):
        sitemap_locs.extend(_LOC_RE.findall(block))

    nested = set(sitemap_locs)
    page_locs = [loc for loc in _LOC_RE.findall(xml) if loc not in nested]
    return sitemap_locs, page_locs


async def collect_sitemap_urls(
    client: httpx.AsyncClient,
    sitemap_urls: list[str],
    base_domain: str,
    max_urls: int,
    *,
    timeout: float = 10.0,
) -> list[str]:
    """Walk every supplied sitemap (and any it indexes) and return same-domain page URLs."""
    discovered: list[str] = []
    seen_urls: set[str] = set()
    visited_sitemaps: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0) for url in sitemap_urls]

    while queue and len(discovered) < max_urls:
        sitemap_url, depth = queue.pop(0)
        normalized = sitemap_url.strip()
        if not normalized or normalized in visited_sitemaps or depth > MAX_SITEMAP_DEPTH:
            continue
        visited_sitemaps.add(normalized)

        try:
            response = await client.get(normalized, timeout=timeout)
        except Exception as exc:
            logger.debug("Sitemap fetch failed for %s: %s", normalized, exc)
            continue

        if response.status_code != 200:
            logger.debug("Sitemap %s returned HTTP %s", normalized, response.status_code)
            continue

        xml = _decode(response)
        if "<loc" not in xml.lower():
            continue

        nested, pages = _extract_locs(xml)

        for child in nested[:MAX_SITEMAPS_PER_INDEX]:
            if child not in visited_sitemaps:
                queue.append((child, depth + 1))

        for loc in pages:
            if len(discovered) >= max_urls:
                break
            url = normalize_url(loc)
            if url in seen_urls:
                continue
            if not is_same_domain(url, base_domain) or not is_probably_page(url):
                continue
            seen_urls.add(url)
            discovered.append(url)

    logger.info(
        "Sitemap discovery found %d URLs across %d sitemap document(s).",
        len(discovered), len(visited_sitemaps),
    )
    return discovered


def default_sitemap_candidates(start_url: str) -> list[str]:
    """Conventional sitemap locations to probe when robots.txt names none."""
    parsed = urlparse(start_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return [
        f"{root}/sitemap.xml",
        f"{root}/sitemap_index.xml",
        f"{root}/sitemap-index.xml",
        f"{root}/sitemap.xml.gz",
        f"{root}/wp-sitemap.xml",
    ]
