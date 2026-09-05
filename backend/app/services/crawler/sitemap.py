"""Sitemap discovery and parsing.

Handles the shapes found in the wild: a plain ``<urlset>``, a ``<sitemapindex>`` pointing at more
sitemaps (recursively, with a depth cap), and gzip-compressed variants — with or without a
``.gz`` extension, since many servers serve compressed bytes from a plain ``.xml`` path.

Parsing is done with an XML parser so that namespaces, CDATA and entities are handled correctly.
A malformed document falls back to a regex sweep rather than being discarded: a surprising number
of production sitemaps are not well-formed XML, and refusing to read them would throw away the
single best source of URLs a site offers. Whichever path is used is reported, so a discrepancy
can be explained.
"""

from __future__ import annotations

import gzip
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ...utils.url_utils import is_probably_page, is_same_domain, normalize_url

logger = logging.getLogger(__name__)

MAX_SITEMAP_DEPTH = 4
MAX_SITEMAPS_PER_INDEX = 50
#: Sitemaps are capped at 50 MB uncompressed by the specification.
MAX_SITEMAP_BYTES = 60 * 1024 * 1024

_LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(https?://[^<\]\s]+)", re.IGNORECASE)
_SITEMAP_BLOCK_RE = re.compile(r"<sitemap[\s>].*?</sitemap>", re.IGNORECASE | re.DOTALL)


@dataclass
class SitemapEntry:
    """One ``<url>`` record, with the optional metadata the specification defines."""

    loc: str
    lastmod: str | None = None
    changefreq: str | None = None
    priority: float | None = None
    source_sitemap: str | None = None


@dataclass
class SitemapResult:
    """Everything one sitemap walk produced, including what went wrong."""

    entries: list[SitemapEntry] = field(default_factory=list)
    sitemaps_fetched: list[str] = field(default_factory=list)
    sitemaps_failed: dict[str, str] = field(default_factory=dict)
    #: URLs skipped and why — off-domain, an asset, or a duplicate.
    skipped: dict[str, int] = field(default_factory=dict)
    used_fallback_parser: bool = False
    truncated: bool = False

    @property
    def urls(self) -> list[str]:
        return [entry.loc for entry in self.entries]

    def note_skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _decode(response: httpx.Response) -> str:
    """Return sitemap text, transparently decompressing gzip regardless of the extension."""
    content = response.content[:MAX_SITEMAP_BYTES]
    if content[:2] == b"\x1f\x8b":  # gzip magic number
        try:
            content = gzip.decompress(content)
        except OSError:
            return response.text
        return content.decode("utf-8", errors="replace")
    return response.text


def _float_or_none(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None


def parse_sitemap_document(xml: str, source: str | None = None) -> tuple[list[SitemapEntry], list[str], bool]:
    """Parse one sitemap into (url entries, nested sitemap URLs, used_fallback).

    The XML parser is tried first so namespaces and CDATA are handled properly; only if it yields
    nothing from a document that clearly contains ``<loc>`` do we fall back to regex.
    """
    entries: list[SitemapEntry] = []
    nested: list[str] = []
    used_fallback = False

    if "<loc" not in xml.lower():
        return entries, nested, used_fallback

    try:
        # "xml" uses lxml-xml, which is namespace-aware; tag lookups below are namespace-agnostic
        # because BeautifulSoup matches on the local name.
        soup = BeautifulSoup(xml, "xml")
    except Exception as exc:
        logger.debug("XML parse failed for %s: %s", source, exc)
        soup = None

    if soup is not None:
        for node in soup.find_all("sitemap"):
            loc = node.find("loc")
            if loc and loc.get_text(strip=True):
                nested.append(loc.get_text(strip=True))

        for node in soup.find_all("url"):
            loc = node.find("loc")
            if not loc or not loc.get_text(strip=True):
                continue
            lastmod = node.find("lastmod")
            changefreq = node.find("changefreq")
            priority = node.find("priority")
            entries.append(
                SitemapEntry(
                    loc=loc.get_text(strip=True),
                    lastmod=lastmod.get_text(strip=True) if lastmod else None,
                    changefreq=changefreq.get_text(strip=True).lower() if changefreq else None,
                    priority=_float_or_none(priority.get_text(strip=True) if priority else None),
                    source_sitemap=source,
                )
            )

    if not entries and not nested:
        # Malformed XML — recover what we can rather than losing the whole document.
        used_fallback = True
        nested_locs: set[str] = set()
        for block in _SITEMAP_BLOCK_RE.findall(xml):
            for loc in _LOC_RE.findall(block):
                nested_locs.add(loc)
        nested = list(nested_locs)
        entries = [
            SitemapEntry(loc=loc, source_sitemap=source)
            for loc in _LOC_RE.findall(xml)
            if loc not in nested_locs
        ]

    return entries, nested, used_fallback


async def collect_sitemap_entries(
    client: httpx.AsyncClient,
    sitemap_urls: list[str],
    base_domain: str,
    max_urls: int | None,
    *,
    timeout: float = 10.0,
) -> SitemapResult:
    """Walk every supplied sitemap (and any it indexes), returning same-domain page entries."""
    result = SitemapResult()
    seen_urls: set[str] = set()
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0) for url in sitemap_urls]

    while queue:
        if max_urls is not None and len(result.entries) >= max_urls:
            result.truncated = True
            break

        sitemap_url, depth = queue.pop(0)
        normalised = sitemap_url.strip()
        if not normalised or normalised in visited or depth > MAX_SITEMAP_DEPTH:
            continue
        visited.add(normalised)

        try:
            response = await client.get(normalised, timeout=timeout)
        except Exception as exc:
            result.sitemaps_failed[normalised] = f"{type(exc).__name__}: {exc}"[:200]
            logger.debug("Sitemap fetch failed for %s: %s", normalised, exc)
            continue

        if response.status_code != 200:
            result.sitemaps_failed[normalised] = f"HTTP {response.status_code}"
            continue

        result.sitemaps_fetched.append(normalised)
        entries, nested, used_fallback = parse_sitemap_document(_decode(response), normalised)
        result.used_fallback_parser = result.used_fallback_parser or used_fallback

        for child in nested[:MAX_SITEMAPS_PER_INDEX]:
            if child not in visited:
                queue.append((child, depth + 1))

        for entry in entries:
            if max_urls is not None and len(result.entries) >= max_urls:
                result.truncated = True
                break
            try:
                url = normalize_url(entry.loc)
            except Exception:
                result.note_skip("malformed")
                continue
            if url in seen_urls:
                result.note_skip("duplicate")
                continue
            if not is_same_domain(url, base_domain):
                result.note_skip("off_domain")
                continue
            if not is_probably_page(url):
                result.note_skip("asset")
                continue
            seen_urls.add(url)
            entry.loc = url
            result.entries.append(entry)

    logger.info(
        "Sitemap discovery: %d URLs from %d document(s); %d failed; skipped %s%s",
        len(result.entries),
        len(result.sitemaps_fetched),
        len(result.sitemaps_failed),
        result.skipped or "none",
        " (regex fallback used)" if result.used_fallback_parser else "",
    )
    return result


async def collect_sitemap_urls(
    client: httpx.AsyncClient,
    sitemap_urls: list[str],
    base_domain: str,
    max_urls: int | None,
    *,
    timeout: float = 10.0,
) -> list[str]:
    """URL-only convenience wrapper kept for the crawl frontier."""
    result = await collect_sitemap_entries(
        client, sitemap_urls, base_domain, max_urls, timeout=timeout
    )
    return result.urls


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
        f"{root}/sitemap/sitemap.xml",
    ]
