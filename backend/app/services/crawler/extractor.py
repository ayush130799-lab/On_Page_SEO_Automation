"""HTML → structured page signals.

Everything here is derived from the parsed DOM, never from regex over raw HTML. Where a value
cannot be determined from static HTML alone (CSS-class-driven visibility, for example) that is
stated explicitly rather than guessed at.

WORD-COUNT METHODOLOGY
----------------------
Three counts are produced, because "how many words does this page have" has three defensible
answers and different tools pick different ones:

``raw_word_count``
    Every word in the document body after removing elements that never render as prose:
    ``script``, ``style``, ``template``, ``svg``, ``iframe``, ``noscript``, and HTML comments.
    This is the widest measure.

``visible_word_count``
    ``raw`` minus elements hidden from the user: the ``hidden`` attribute,
    ``aria-hidden="true"``, and inline ``style`` containing ``display:none`` or
    ``visibility:hidden``. Class-driven hiding is **not** detectable without a browser and is
    documented as a known limit.

``main_content_word_count`` (aliased as ``word_count``)
    ``visible`` minus site chrome: ``nav``, ``header``, ``footer``, ``aside``, ``form``,
    ``figcaption``, ``dialog``. When the page has a ``<main>`` or ``<article>`` element, only
    that subtree is measured — that is the strongest available signal for "the content of this
    page" and it is what thin-content rules should judge.

``word_count`` is the main-content measure because that is what a thin-content rule must judge.
All three are stored so a discrepancy against another tool can be explained rather than argued.

Counting itself is whitespace tokenisation of the extracted text — never ``len(html)`` and never
a split of raw markup.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString

from ...utils.url_utils import absolute_url, content_hash, is_same_domain

logger = logging.getLogger(__name__)

MAX_STORED_CONTENT = 20_000
MAX_STORED_LINKS = 500
MAX_STORED_LINK_RECORDS = 300
MAX_STORED_IMAGES = 300

#: Never rendered as prose. Removed before any text measurement.
_NON_RENDERING_TAGS = ("script", "style", "template", "svg", "iframe", "noscript")

#: Site chrome — excluded from the main-content measure only.
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form", "figcaption", "dialog")

#: Inline styles that hide an element outright (including 1px visually clipped sr-only/SEO fallbacks).
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0)\s*(?:;|$)"
    r"|clip\s*:\s*rect\s*\(\s*0"
    r"|clip-path\s*:\s*inset\s*\(\s*100%"
    r"|(?:width|height)\s*:\s*(?:0|1)px",
    re.IGNORECASE,
)

#: A tracking pixel is identified by its *dimensions*, or by a filename that is unambiguously a
#: beacon endpoint. Substring matching on words like "collect" or "stats" was previously dropping
#: real content images (``/media/collections/dress.jpg``), so matching is anchored to a path
#: segment or filename stem instead.
_PIXEL_FILENAME_RE = re.compile(
    r"(?:^|[/_-])(?:pixel|beacon|spacer|blank|clear|1x1|px)(?:[._-]|$)", re.IGNORECASE
)
#: "pixel" and "beacon" must end the path segment or be followed by a separator - without the
#: lookahead, /photos/pixelated-art.jpg matched and a real content image was written off as a
#: beacon, silently lowering image_count and hiding a genuine missing-ALT problem.
_BEACON_PATH_RE = re.compile(
    r"/(?:__utm\.gif"
    r"|pixel(?=[./?_-]|$)"
    r"|beacon(?=[./?_-]|$)"
    r"|track(?:ing)?/"
    r"|collect\?"
    r"|b/ss/"
    r"|p\.gif"
    r"|noscript\.gif)",
    re.IGNORECASE,
)


@dataclass
class LinkRecord:
    """One ``<a href>`` with everything the link rules and reports need."""

    url: str
    anchor_text: str = ""
    is_internal: bool = False
    nofollow: bool = False
    sponsored: bool = False
    ugc: bool = False
    rel: list[str] = field(default_factory=list)


@dataclass
class ImageRecord:
    """One ``<img>`` element as declared in the DOM."""

    src: str
    alt: str | None = None          # None = attribute absent; "" = explicitly decorative
    has_srcset: bool = False
    width: str | None = None
    height: str | None = None
    loading: str | None = None
    is_tracking_pixel: bool = False


@dataclass
class ExtractedPage:
    """Everything the rule engine needs about one page."""

    url: str
    status_code: int

    # ── Title / meta ─────────────────────────────────────────────────────────
    title: str | None = None
    title_count: int = 0                # <title> elements inside <head>
    meta_description: str | None = None
    meta_description_count: int = 0
    meta_robots: str | None = None
    meta_robots_count: int = 0

    # ── Canonical ────────────────────────────────────────────────────────────
    canonical_url: str | None = None    # resolved absolute URL
    canonical_raw: str | None = None    # raw href, before resolution
    canonical_count: int = 0
    canonical_status: str = "missing"   # see CANONICAL_STATUSES below

    lang: str | None = None
    hreflang: list[dict[str, str]] = field(default_factory=list)
    has_viewport: bool = False

    # ── Headings (h1-h6, all counted against the same DOM state) ─────────────
    h1: str | None = None
    h1_count: int = 0
    h2_count: int = 0
    h3_count: int = 0
    h4_count: int = 0
    h5_count: int = 0
    h6_count: int = 0
    empty_heading_count: int = 0
    headings: list[dict[str, str]] = field(default_factory=list)

    # ── Content ──────────────────────────────────────────────────────────────
    content: str = ""                   # truncated main-content text, for storage
    word_count: int = 0                 # == main_content_word_count
    raw_word_count: int = 0
    visible_word_count: int = 0
    main_content_word_count: int = 0
    content_scope: str = "body"         # "main" | "article" | "body"
    content_hash: str | None = None

    # ── Images ───────────────────────────────────────────────────────────────
    image_count: int = 0                # content images (tracking pixels excluded)
    missing_alt_count: int = 0          # alt attribute absent
    empty_alt_count: int = 0            # alt="" — intentionally decorative
    images_without_dimensions: int = 0
    tracking_pixel_count: int = 0
    images: list[ImageRecord] = field(default_factory=list)

    # ── Links ────────────────────────────────────────────────────────────────
    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    links: list[LinkRecord] = field(default_factory=list)
    nofollow_link_count: int = 0
    sponsored_link_count: int = 0
    ugc_link_count: int = 0
    non_http_link_count: int = 0        # mailto:, tel:, javascript:, empty href
    broken_link_count: int = 0

    pagination_next: str | None = None
    pagination_prev: str | None = None

    # ── Structured data / social ─────────────────────────────────────────────
    has_structured_data: bool = False
    structured_data_types: list[str] = field(default_factory=list)
    structured_data_invalid: bool = False
    structured_data_formats: list[str] = field(default_factory=list)  # json-ld|microdata|rdfa
    json_ld_error: str | None = None
    has_open_graph: bool = False
    has_twitter_card: bool = False

    # ── Response-level ───────────────────────────────────────────────────────
    x_robots_tag: str | None = None
    content_type: str | None = None
    charset: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    # ── Crawl metadata (filled by the orchestrator) ──────────────────────────
    final_url: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    was_rendered: bool = False
    render_error: str | None = None
    response_time_ms: int | None = None
    content_bytes: int | None = None
    crawl_error: str | None = None
    #: "ok" | "partial" | "render_failed" | "failed" — never silently "ok" after a failure.
    crawl_quality: str = "ok"
    extraction_errors: list[str] = field(default_factory=list)
    inbound_internal_links: int = 0

    #: How every headline value was obtained: the selector used, how many nodes matched, the raw
    #: attribute text before normalisation, and any decision taken. Recorded during extraction so
    #: the debug endpoint reports what the crawler actually did rather than re-deriving it and
    #: risking a second, divergent implementation.
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)

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

    @property
    def is_usable(self) -> bool:
        """False when the fetch failed — no SEO conclusion may be drawn from this page."""
        return self.crawl_quality not in ("failed",) and self.status_code != 0

    @property
    def has_document(self) -> bool:
        """True when this observation actually parsed a document, so its signals may be stored.

        A failed fetch, a non-2xx response, or a body we could not parse carries no document.
        Writing its empty fields over the previous crawl's values would turn one transient
        timeout into a page that appears to have lost its title, headings and content — a failed
        crawl silently becoming fabricated SEO data.
        """
        return (
            200 <= (self.status_code or 0) < 300
            and self.crawl_quality in ("ok", "render_failed")
        )


#: Canonical states, kept explicit so the dashboard never has to infer one.
CANONICAL_STATUSES = (
    "missing",        # no <link rel="canonical">
    "empty",          # tag present, href empty or whitespace
    "self",           # resolves to this page
    "other",          # resolves elsewhere
    "relative",       # declared relative (still resolved into canonical_url)
    "invalid",        # href present but not a usable http(s) URL
    "multiple",       # more than one canonical tag — conflicting
)


# ── Small helpers ───────────────────────────────────────────────────────────


def _normalise_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _count_words(text: str) -> int:
    """Whitespace tokenisation of already-extracted text. Never operates on markup."""
    return len(text.split()) if text else 0


def _is_hidden(tag) -> bool:
    """True when an element is hidden by an attribute or an inline style.

    Class-driven hiding cannot be resolved without a browser; that limitation is documented in
    the module docstring rather than guessed at.
    """
    if tag.has_attr("hidden"):
        return True
    if (tag.get("aria-hidden") or "").strip().lower() == "true":
        return True
    tag_id = (tag.get("id") or "").strip().lower()
    if tag_id in ("seo-fallback", "sr-only"):
        return True
    tag_classes = tag.get("class") or []
    if any(c in ("sr-only", "visually-hidden") for c in tag_classes):
        return True
    style = tag.get("style") or ""
    return bool(_HIDDEN_STYLE_RE.search(style))


def _meta_tags(soup: BeautifulSoup, name: str) -> list[Any]:
    """Every meta tag matching ``name`` on either the name or property attribute."""
    lowered = name.lower()
    return [
        tag
        for tag in soup.find_all("meta")
        if (tag.get("name") or "").strip().lower() == lowered
        or (tag.get("property") or "").strip().lower() == lowered
    ]


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    for tag in _meta_tags(soup, name):
        value = (tag.get("content") or "").strip()
        if value:
            return value
    return None


def is_tracking_pixel(img_tag) -> bool:
    """True only for images that are demonstrably beacons, not merely oddly named.

    Detection is by declared 1x1 dimensions, or by a filename/path that is unambiguously a
    tracking endpoint. Earlier substring matching (``collect``, ``stats``, ``log``) silently
    discarded ordinary content images such as ``/media/collections/dress.jpg``.
    """
    try:
        width = int(str(img_tag.get("width", "")).strip() or 0)
        height = int(str(img_tag.get("height", "")).strip() or 0)
        if 0 < width <= 1 and 0 < height <= 1:
            return True
    except (ValueError, TypeError):
        pass

    src = (img_tag.get("src") or img_tag.get("data-src") or "").strip()
    if not src:
        return False

    path = src.split("?", 1)[0]
    filename = path.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    if _BEACON_PATH_RE.search(src):
        return True
    return bool(_PIXEL_FILENAME_RE.search(f"/{stem}"))


# ── Structured data ─────────────────────────────────────────────────────────


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


def _extract_structured_data(soup: BeautifulSoup) -> dict[str, Any]:
    """Detect JSON-LD, Microdata and RDFa. Presence is never inferred from stray text."""
    types: list[str] = []
    formats: list[str] = []
    invalid = False
    error: str | None = None

    scripts = [
        s
        for s in soup.find_all("script")
        if "ld+json" in (s.get("type") or "").lower()
    ]
    if scripts:
        formats.append("json-ld")
    for script in scripts:
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            types.extend(_collect_types(json.loads(raw)))
        except (ValueError, TypeError) as exc:
            invalid = True
            if error is None:
                error = str(exc)[:200]

    # Microdata: itemscope/itemtype.
    microdata = soup.find_all(attrs={"itemtype": True})
    if microdata:
        formats.append("microdata")
        for node in microdata:
            itemtype = node.get("itemtype")
            if isinstance(itemtype, str) and itemtype.strip():
                types.append(itemtype.strip().rstrip("/").rsplit("/", 1)[-1])

    # RDFa: typeof, with vocab/property as supporting evidence.
    rdfa = soup.find_all(attrs={"typeof": True})
    if rdfa:
        formats.append("rdfa")
        for node in rdfa:
            typeof = node.get("typeof")
            if isinstance(typeof, str) and typeof.strip():
                for token in typeof.split():
                    types.append(token.rstrip("/").rsplit("/", 1)[-1].split(":")[-1])

    return {
        "present": bool(scripts or microdata or rdfa),
        "types": list(dict.fromkeys(t for t in types if t)),
        "invalid": invalid,
        "formats": list(dict.fromkeys(formats)),
        "error": error,
    }


# ── Text measurement ────────────────────────────────────────────────────────


def _measure_text(soup: BeautifulSoup) -> dict[str, Any]:
    """Produce the three documented word counts from one parsed document.

    Works on independent copies so that removing chrome for the main-content measure cannot
    affect any other extraction — the previous implementation mutated one shared tree, which is
    why H1 and H2 ended up counted against different DOM states.
    """
    body = soup.body or soup

    # 1. raw: strip only elements that never render.
    raw_soup = BeautifulSoup(str(body), "lxml")
    for comment in raw_soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    for tag in raw_soup(_NON_RENDERING_TAGS):
        tag.decompose()
    raw_text = _normalise_whitespace(raw_soup.get_text(" ", strip=True))

    # 2. visible: additionally remove hidden elements.
    visible_soup = BeautifulSoup(str(raw_soup), "lxml")
    for tag in visible_soup.find_all(True):
        # find_all returns a snapshot. Decomposing a hidden element also decomposes its
        # descendants, which are still in that list and whose .attrs is then None - reading one
        # raised TypeError and cost the page all three word counts.
        if tag.decomposed:
            continue
        if _is_hidden(tag):
            tag.decompose()
    visible_text = _normalise_whitespace(visible_soup.get_text(" ", strip=True))

    # 3. main content: prefer <main>/<article>, else visible minus chrome.
    scope = "body"
    main_soup = BeautifulSoup(str(visible_soup), "lxml")
    container = main_soup.find("main") or main_soup.find("article")
    if container is not None:
        scope = container.name
        for tag in container(_CHROME_TAGS):
            tag.decompose()
        main_text = _normalise_whitespace(container.get_text(" ", strip=True))
    else:
        for tag in main_soup(_CHROME_TAGS):
            tag.decompose()
        main_text = _normalise_whitespace(main_soup.get_text(" ", strip=True))

    return {
        "raw_text": raw_text,
        "visible_text": visible_text,
        "main_text": main_text,
        "scope": scope,
        "raw_word_count": _count_words(raw_text),
        "visible_word_count": _count_words(visible_text),
        "main_content_word_count": _count_words(main_text),
    }


# ── Main entry point ────────────────────────────────────────────────────────


def extract_page(
    url: str,
    html: str,
    base_domain: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> ExtractedPage:
    """Parse HTML into an :class:`ExtractedPage`.

    ``headers`` supplies response-level signals (``X-Robots-Tag``, content type, charset) so that
    indexability can consider them alongside the DOM.
    """
    errors: list[str] = []
    headers = headers or {}
    soup = BeautifulSoup(html or "", "lxml")

    # lxml is the primary parser because it recovers unclosed tags well, but it throws the entire
    # document away when the response opens with a stray closing tag or other junk before the
    # doctype - output browsers render without complaint. Reparsing that case with html.parser
    # recovers the document instead of reporting every field as missing.
    if html and html.strip() and soup.find(True) is None:
        soup = BeautifulSoup(html, "html.parser")
        errors.append("lxml_returned_empty_document: reparsed with html.parser")

    prov: dict[str, dict[str, Any]] = {}

    def record(name: str, selector: str, matched: int, raw: Any = None, note: str | None = None):
        """Note where one value came from, so any figure can be traced back to the DOM."""
        entry: dict[str, Any] = {"selector": selector, "matched": matched}
        if raw is not None:
            entry["raw"] = str(raw)[:500]
        if note:
            entry["note"] = note
        prov[name] = entry

    # ── Title: <head> only. An inline <svg><title> is an accessible icon label,
    #    not the page title, and must never be mistaken for one.
    head = soup.head
    title_tags = [t for t in head.find_all("title", recursive=False)] if head else []
    if not title_tags and head:
        # Some documents nest <title> a level deeper in <head>; SVG cannot appear there.
        title_tags = [t for t in head.find_all("title") if t.find_parent("svg") is None]
    title_text = title_tags[0].get_text(" ", strip=True) if title_tags else None
    title = _normalise_whitespace(title_text) or None if title_text is not None else None
    record(
        "title", "head > title (excluding svg > title)", len(title_tags), title_text,
        "First <title> in <head> wins; later duplicates are counted, not merged."
        if len(title_tags) > 1 else None,
    )

    description_tags = _meta_tags(soup, "description")
    meta_description = None
    for tag in description_tags:
        value = _normalise_whitespace(tag.get("content") or "")
        if value:
            meta_description = value
            break
    record(
        "meta_description", 'meta[name="description" i]', len(description_tags),
        description_tags[0].get("content") if description_tags else None,
        "First non-empty content attribute wins." if len(description_tags) > 1 else None,
    )

    robots_tags = _meta_tags(soup, "robots")
    meta_robots = _meta_content(soup, "robots")
    record(
        "meta_robots", 'meta[name="robots" i]', len(robots_tags),
        robots_tags[0].get("content") if robots_tags else None,
    )

    # ── Canonical ────────────────────────────────────────────────────────────
    canonical_tags = soup.find_all("link", rel=lambda r: bool(r) and "canonical" in r)
    canonical_count = len(canonical_tags)
    canonical_url: str | None = None
    canonical_raw: str | None = None
    canonical_status = "missing"

    if canonical_tags:
        raw_href = (canonical_tags[0].get("href") or "").strip()
        canonical_raw = raw_href or None
        if not raw_href:
            canonical_status = "empty"
        else:
            resolved = absolute_url(url, raw_href)
            if resolved is None:
                canonical_status = "invalid"
            else:
                canonical_url = resolved
                from ...utils.url_utils import normalize_url

                if normalize_url(resolved) == normalize_url(url):
                    canonical_status = "self"
                elif not raw_href.lower().startswith(("http://", "https://")):
                    canonical_status = "relative"
                else:
                    canonical_status = "other"
        if canonical_count > 1:
            canonical_status = "multiple"
    record(
        "canonical", 'link[rel~="canonical"]', canonical_count, canonical_raw,
        f"status={canonical_status}"
        + (f"; resolved against {url}" if canonical_status == "relative" else ""),
    )

    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").strip() or None if html_tag else None
    record("lang", "html[lang]", 1 if html_tag is not None else 0, lang)

    hreflang: list[dict[str, str]] = []
    for link in soup.find_all("link", rel=lambda r: bool(r) and "alternate" in r):
        if not link.get("hreflang"):
            continue  # RSS/Atom alternates carry no hreflang
        link_type = (link.get("type") or "").lower()
        if any(token in link_type for token in ("rss", "atom", "xml")):
            continue
        hreflang.append(
            {
                "lang": (link.get("hreflang") or "").strip(),
                "href": (link.get("href") or "").strip(),
            }
        )

    viewport_tag = soup.find(
        "meta", attrs={"name": lambda x: bool(x) and x.lower() == "viewport"}
    )
    has_viewport = bool(viewport_tag and (viewport_tag.get("content") or "").strip())
    record(
        "has_viewport", 'meta[name="viewport" i]', 1 if viewport_tag is not None else 0,
        viewport_tag.get("content") if viewport_tag is not None else None,
    )
    record(
        "hreflang", 'link[rel~="alternate"][hreflang]', len(hreflang), None,
        "RSS/Atom alternates and alternates without an hreflang attribute are excluded.",
    )

    og_tags = [
        t
        for t in soup.find_all("meta")
        if (t.get("property") or t.get("name") or "").strip().lower().startswith("og:")
    ]
    has_open_graph = bool(og_tags)
    has_twitter_card = bool(_meta_content(soup, "twitter:card"))

    structured = _extract_structured_data(soup)
    record(
        "structured_data",
        'script[type="application/ld+json"], [itemscope][itemtype], [typeof]',
        len(structured["types"]),
        None,
        "formats=" + (",".join(structured["formats"]) or "none")
        + (f"; json_ld_error={structured['error']}" if structured["error"] else ""),
    )

    # ── Headings: h1-h6, all measured against the SAME document state ────────
    heading_counts: dict[str, int] = {}
    empty_headings = 0
    heading_records: list[dict[str, str]] = []
    h1_texts: list[str] = []

    for level in range(1, 7):
        tags = soup.find_all(f"h{level}")
        # Prefer visible headings over hidden/fallback headings
        visible_tags = [
            t
            for t in tags
            if not (
                _is_hidden(t)
                or any(
                    _is_hidden(p)
                    for p in t.parents
                    if p.name not in ("[document]", "html", "body")
                )
            )
        ]
        active_tags = visible_tags if visible_tags else tags
        heading_counts[f"h{level}"] = len(active_tags)
        for tag in active_tags:
            text = _normalise_whitespace(tag.get_text(" ", strip=True))
            if not text:
                empty_headings += 1
                continue
            if level == 1:
                h1_texts.append(text)
            if len(heading_records) < 200:
                heading_records.append({"level": f"h{level}", "text": text})

    for level in range(1, 7):
        record(
            f"h{level}_count", f"h{level}", heading_counts[f"h{level}"],
            h1_texts[0] if level == 1 and h1_texts else None,
            "All six levels are counted against one unmodified document."
            if level == 1 else None,
        )
    record(
        "empty_heading_count", "h1-h6 with no text content", empty_headings, None,
        "Headings whose text is empty after whitespace normalisation.",
    )

    # ── Images ───────────────────────────────────────────────────────────────
    image_records: list[ImageRecord] = []
    missing_alt = 0
    empty_alt = 0
    no_dimensions = 0
    pixels = 0

    for img in soup.find_all("img"):
        pixel = is_tracking_pixel(img)
        alt_value = img.get("alt")
        image_record = ImageRecord(
            src=(img.get("src") or img.get("data-src") or "").strip(),
            alt=alt_value,
            has_srcset=bool(img.get("srcset")),
            width=img.get("width"),
            height=img.get("height"),
            loading=img.get("loading"),
            is_tracking_pixel=pixel,
        )
        if len(image_records) < MAX_STORED_IMAGES:
            image_records.append(image_record)

        if pixel:
            pixels += 1
            continue  # beacons are not content images

        if alt_value is None:
            missing_alt += 1
        elif alt_value.strip() == "":
            empty_alt += 1
        if not (img.get("width") and img.get("height")):
            no_dimensions += 1

    content_images = [r for r in image_records if not r.is_tracking_pixel]
    image_count = sum(1 for img in soup.find_all("img") if not is_tracking_pixel(img))
    record(
        "image_count", "img", len(soup.find_all("img")), None,
        f"{pixels} tracking pixel(s) excluded; CSS backgrounds and inline SVG are not <img> "
        f"elements and are never counted.",
    )
    record(
        "missing_alt_count", "img without an alt attribute", missing_alt, None,
        f'Distinct from empty_alt_count={empty_alt} (alt="", a declared decorative image).',
    )

    # ── Links ────────────────────────────────────────────────────────────────
    internal: list[str] = []
    external: list[str] = []
    seen_internal: set[str] = set()
    seen_external: set[str] = set()
    link_records: list[LinkRecord] = []
    nofollow = sponsored = ugc = non_http = 0

    for anchor in soup.find_all("a", href=True):
        raw_href = (anchor.get("href") or "").strip()
        target = absolute_url(url, raw_href)
        if not target:
            # mailto:, tel:, javascript:, #fragment, empty, or malformed — never a crawl target.
            non_http += 1
            continue

        rel_attr = anchor.get("rel") or []
        rel_values = [
            r.lower() for r in (rel_attr if isinstance(rel_attr, list) else str(rel_attr).split())
        ]
        is_nofollow = "nofollow" in rel_values
        is_sponsored = "sponsored" in rel_values
        is_ugc = "ugc" in rel_values
        nofollow += is_nofollow
        sponsored += is_sponsored
        ugc += is_ugc

        # Classification is by normalised host, never string containment.
        internal_link = is_same_domain(target, base_domain)

        if len(link_records) < MAX_STORED_LINK_RECORDS:
            link_records.append(
                LinkRecord(
                    url=target,
                    anchor_text=_normalise_whitespace(anchor.get_text(" ", strip=True))[:300],
                    is_internal=internal_link,
                    nofollow=is_nofollow,
                    sponsored=is_sponsored,
                    ugc=is_ugc,
                    rel=rel_values,
                )
            )

        if internal_link:
            if target not in seen_internal and len(internal) < MAX_STORED_LINKS:
                seen_internal.add(target)
                internal.append(target)
        elif target not in seen_external and len(external) < MAX_STORED_LINKS:
            seen_external.add(target)
            external.append(target)

    record(
        "links", "a[href]", len(soup.find_all("a", href=True)), None,
        f"internal={len(internal)} external={len(external)} non_http={non_http}; classified by "
        f"normalised registrable host against '{base_domain}', never string containment.",
    )

    # ── Pagination ───────────────────────────────────────────────────────────
    pagination_next: str | None = None
    pagination_prev: str | None = None
    for link in soup.find_all("link"):
        rel_attr = link.get("rel") or []
        rel_lower = {
            r.lower() for r in (rel_attr if isinstance(rel_attr, list) else str(rel_attr).split())
        }
        href = (link.get("href") or "").strip()
        if not href:
            continue
        resolved = absolute_url(url, href)
        if not resolved:
            continue
        if "next" in rel_lower and pagination_next is None:
            pagination_next = resolved
            if is_same_domain(resolved, base_domain) and resolved not in seen_internal:
                seen_internal.add(resolved)
                if len(internal) < MAX_STORED_LINKS:
                    internal.append(resolved)
        elif "prev" in rel_lower and pagination_prev is None:
            pagination_prev = resolved
            if is_same_domain(resolved, base_domain) and resolved not in seen_internal:
                seen_internal.add(resolved)
                if len(internal) < MAX_STORED_LINKS:
                    internal.append(resolved)

    # ── Text ─────────────────────────────────────────────────────────────────
    try:
        measured = _measure_text(soup)
    except Exception as exc:  # never lose a page to a text-measurement problem
        logger.warning("Text measurement failed for %s: %s", url, exc)
        errors.append(f"text_measurement: {type(exc).__name__}")
        measured = {
            "raw_text": "", "visible_text": "", "main_text": "", "scope": "body",
            "raw_word_count": 0, "visible_word_count": 0, "main_content_word_count": 0,
        }

    record(
        "word_count", measured["scope"], measured["main_content_word_count"], None,
        f"main_content={measured['main_content_word_count']} (scope <{measured['scope']}>, "
        f"chrome removed) | visible={measured['visible_word_count']} (body, script/style/hidden "
        f"removed) | raw={measured['raw_word_count']} (all text nodes). word_count is the "
        f"main-content figure.",
    )

    content_type = headers.get("content-type") or headers.get("Content-Type")
    charset = None
    if content_type and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()

    return ExtractedPage(
        url=url,
        status_code=status_code,
        title=title,
        title_count=len(title_tags),
        meta_description=meta_description,
        meta_description_count=len(description_tags),
        meta_robots=meta_robots,
        meta_robots_count=len(robots_tags),
        canonical_url=canonical_url,
        canonical_raw=canonical_raw,
        canonical_count=canonical_count,
        canonical_status=canonical_status,
        lang=lang,
        hreflang=hreflang,
        has_viewport=has_viewport,
        h1=" | ".join(h1_texts) or None,
        h1_count=heading_counts["h1"],
        h2_count=heading_counts["h2"],
        h3_count=heading_counts["h3"],
        h4_count=heading_counts["h4"],
        h5_count=heading_counts["h5"],
        h6_count=heading_counts["h6"],
        empty_heading_count=empty_headings,
        headings=heading_records[:100],
        content=measured["main_text"][:MAX_STORED_CONTENT],
        word_count=measured["main_content_word_count"],
        raw_word_count=measured["raw_word_count"],
        visible_word_count=measured["visible_word_count"],
        main_content_word_count=measured["main_content_word_count"],
        content_scope=measured["scope"],
        content_hash=content_hash(measured["main_text"]),
        image_count=image_count,
        missing_alt_count=missing_alt,
        empty_alt_count=empty_alt,
        images_without_dimensions=no_dimensions,
        tracking_pixel_count=pixels,
        images=content_images,
        internal_links=internal,
        external_links=external,
        links=link_records,
        nofollow_link_count=nofollow,
        sponsored_link_count=sponsored,
        ugc_link_count=ugc,
        non_http_link_count=non_http,
        pagination_next=pagination_next,
        pagination_prev=pagination_prev,
        has_structured_data=structured["present"],
        structured_data_types=structured["types"],
        structured_data_invalid=structured["invalid"],
        structured_data_formats=structured["formats"],
        json_ld_error=structured["error"],
        has_open_graph=has_open_graph,
        has_twitter_card=has_twitter_card,
        x_robots_tag=headers.get("x-robots-tag") or headers.get("X-Robots-Tag"),
        content_type=content_type,
        charset=charset,
        headers=dict(headers),
        extraction_errors=errors,
        provenance=prov,
    )


def empty_page(url: str, status_code: int, error: str | None = None) -> ExtractedPage:
    """Placeholder for a URL that could not be fetched or returned no HTML.

    ``crawl_quality`` distinguishes a genuine HTTP response we could not parse ("partial") from a
    request that never completed ("failed"). The audit engine must not draw SEO conclusions from
    either — see :meth:`ExtractedPage.is_usable`.
    """
    return ExtractedPage(
        url=url,
        status_code=status_code,
        crawl_error=error,
        final_url=url,
        crawl_quality="failed" if status_code == 0 else "partial",
    )
