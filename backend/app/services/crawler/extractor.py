"""HTML → structured page signals.

Extends the original MVP extractor with the fields the wider rule set needs: hreflang, viewport,
language, JSON-LD types, word count, content hash, nofollow links and image metadata.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from ...utils.url_utils import absolute_url, content_hash, domain_of, is_same_domain

logger = logging.getLogger(__name__)

MAX_STORED_CONTENT = 20_000
MAX_STORED_LINKS = 500


@dataclass
class ExtractedPage:
    """Everything the rule engine needs about one page."""

    url: str
    status_code: int

    title: str | None = None
    meta_description: str | None = None
    meta_robots: str | None = None
    canonical_url: str | None = None
    lang: str | None = None
    hreflang: list[dict[str, str]] = field(default_factory=list)
    has_viewport: bool = False

    h1: str | None = None
    h1_count: int = 0
    h2_count: int = 0
    h3_count: int = 0
    headings: list[dict[str, str]] = field(default_factory=list)

    content: str = ""
    word_count: int = 0
    content_hash: str | None = None

    image_count: int = 0
    missing_alt_count: int = 0
    images_without_dimensions: int = 0

    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    nofollow_link_count: int = 0
    broken_link_count: int = 0

    has_structured_data: bool = False
    structured_data_types: list[str] = field(default_factory=list)
    structured_data_invalid: bool = False
    has_open_graph: bool = False
    has_twitter_card: bool = False

    x_robots_tag: str | None = None
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    # ── Crawl metadata (filled by the orchestrator) ─────────────────────────
    final_url: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    was_rendered: bool = False
    response_time_ms: int | None = None
    content_bytes: int | None = None
    crawl_error: str | None = None
    #: Populated by the site-wide pass — how many other pages link here.
    inbound_internal_links: int = 0

    @property
    def internal_link_count(self) -> int:
        return len(self.internal_links)

    @property
    def external_link_count(self) -> int:
        return len(self.external_links)

    @property
    def robots_directive(self) -> str | None:
        """Alias kept for the rule engine's vocabulary."""
        return self.meta_robots


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": lambda x: bool(x) and x.lower() == name})
    if tag is None:
        tag = soup.find("meta", attrs={"property": lambda x: bool(x) and x.lower() == name})
    if tag is None:
        return None
    value = (tag.get("content") or "").strip()
    return value or None


def _extract_structured_data(soup: BeautifulSoup) -> tuple[bool, list[str], bool]:
    """Return (present, @type values found, any JSON-LD block failed to parse)."""
    types: list[str] = []
    invalid = False

    scripts = soup.find_all("script", type=lambda t: bool(t) and "ld+json" in t.lower())
    for script in scripts:
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            invalid = True
            continue
        types.extend(_collect_types(data))

    microdata = soup.find_all(attrs={"itemtype": True})
    for node in microdata:
        itemtype = node.get("itemtype")
        if isinstance(itemtype, str) and itemtype:
            types.append(itemtype.rstrip("/").rsplit("/", 1)[-1])

    present = bool(scripts or microdata)
    # Preserve first-seen order while removing duplicates.
    return present, list(dict.fromkeys(t for t in types if t)), invalid


def _collect_types(node: Any) -> list[str]:
    """Walk a JSON-LD structure collecting every ``@type`` value."""
    found: list[str] = []
    if isinstance(node, dict):
        value = node.get("@type")
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(v for v in value if isinstance(v, str))
        for child in node.values():
            if isinstance(child, (dict, list)):
                found.extend(_collect_types(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_collect_types(child))
    return found


def extract_page(
    url: str, html: str, base_domain: str, status_code: int
) -> ExtractedPage:
    """Parse HTML into an :class:`ExtractedPage`."""
    soup = BeautifulSoup(html or "", "lxml")

    # Read structured data and social tags before scripts are stripped.
    has_sd, sd_types, sd_invalid = _extract_structured_data(soup)
    has_open_graph = bool(_meta_content(soup, "og:title") or _meta_content(soup, "og:description"))
    has_twitter_card = bool(_meta_content(soup, "twitter:card"))

    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").strip() or None if html_tag else None

    hreflang = [
        {"lang": (link.get("hreflang") or "").strip(), "href": (link.get("href") or "").strip()}
        for link in soup.find_all("link", rel=lambda r: bool(r) and "alternate" in r)
        if link.get("hreflang")
    ]

    viewport_tag = soup.find("meta", attrs={"name": lambda x: bool(x) and x.lower() == "viewport"})
    has_viewport = bool(viewport_tag and (viewport_tag.get("content") or "").strip())

    canonical_tag = soup.find("link", rel=lambda r: bool(r) and "canonical" in r)
    canonical_url = None
    if canonical_tag:
        href = (canonical_tag.get("href") or "").strip()
        if href:
            canonical_url = absolute_url(url, href) or href

    meta_robots = _meta_content(soup, "robots")
    meta_description = _meta_content(soup, "description")

    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else None

    h1_tags = soup.find_all("h1")
    h1_texts = [h.get_text(" ", strip=True) for h in h1_tags if h.get_text(strip=True)]
    headings = [
        {"level": f"h{level}", "text": tag.get_text(" ", strip=True)}
        for level in (1, 2, 3)
        for tag in soup.find_all(f"h{level}")
        if tag.get_text(strip=True)
    ][:100]

    images = soup.find_all("img")
    missing_alt = sum(1 for img in images if not (img.get("alt") or "").strip())
    no_dimensions = sum(1 for img in images if not (img.get("width") and img.get("height")))

    internal: list[str] = []
    external: list[str] = []
    seen_internal: set[str] = set()
    seen_external: set[str] = set()
    nofollow = 0

    for anchor in soup.find_all("a", href=True):
        target = absolute_url(url, anchor["href"])
        if not target:
            continue
        rel = anchor.get("rel") or []
        rel_values = {r.lower() for r in (rel if isinstance(rel, list) else str(rel).split())}
        if "nofollow" in rel_values:
            nofollow += 1
        if is_same_domain(target, base_domain) or domain_of(target) == base_domain:
            if target not in seen_internal and len(internal) < MAX_STORED_LINKS:
                seen_internal.add(target)
                internal.append(target)
        elif target not in seen_external and len(external) < MAX_STORED_LINKS:
            seen_external.add(target)
            external.append(target)

    # Strip non-content elements before measuring readable text.
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    normalised_text = re.sub(r"\s+", " ", text).strip()

    return ExtractedPage(
        url=url,
        status_code=status_code,
        title=title,
        meta_description=meta_description,
        meta_robots=meta_robots,
        canonical_url=canonical_url,
        lang=lang,
        hreflang=hreflang,
        has_viewport=has_viewport,
        h1=" | ".join(h1_texts) or None,
        h1_count=len(h1_tags),
        h2_count=len(soup.find_all("h2")),
        h3_count=len(soup.find_all("h3")),
        headings=headings,
        content=normalised_text[:MAX_STORED_CONTENT],
        word_count=len(normalised_text.split()),
        content_hash=content_hash(normalised_text),
        image_count=len(images),
        missing_alt_count=missing_alt,
        images_without_dimensions=no_dimensions,
        internal_links=internal,
        external_links=external,
        nofollow_link_count=nofollow,
        has_structured_data=has_sd,
        structured_data_types=sd_types,
        structured_data_invalid=sd_invalid,
        has_open_graph=has_open_graph,
        has_twitter_card=has_twitter_card,
    )


def empty_page(url: str, status_code: int, error: str | None = None) -> ExtractedPage:
    """Placeholder for a URL that could not be fetched — it still gets audited and reported."""
    return ExtractedPage(url=url, status_code=status_code, crawl_error=error, final_url=url)
