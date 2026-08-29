"""URL normalisation, identity hashing and SSRF-safe destination checks."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

#: Tracking, session, sorting, view and state parameters stripped during normalisation so duplicate parameter variations are not counted as separate pages.
NON_CONTENT_PARAMS = {
    # Analytics & Ad Tracking
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga", "_gl", "ref", "ref_src",
    "yclid", "igshid", "vero_id", "wickedid", "hsa_acc", "hsa_cam", "fb_action_ids",
    "fb_action_types", "fb_source", "action_object_map", "s_kwcid", "dclid",
    
    # Sessions, Auth & Security Tokens
    "session", "sessionid", "sid", "phpsessid", "jsessionid", "asp.net_sessionid",
    "auth", "authtoken", "token", "access_token", "key", "apikey", "nonce", "_t",

    # Redirects & Navigation Targets
    "redirect", "redirect_to", "redirect_uri", "next", "return", "return_to",
    "return_url", "dest", "destination", "target", "goto", "continue", "forward", "back",

    # View, Layout, Display & Theme State
    "view", "mode", "layout", "display", "theme", "style", "format", "output",
    "preview", "tab", "popup", "modal", "print", "export", "device", "lang_choice",

    # Sort, Order & Filter State
    "sort", "order", "orderby", "dir", "direction", "sort_by", "sort_order", "sortby", "sortorder",

    # Actions, Interactions & Shares
    "action", "do", "add-to-cart", "replytocom", "share", "social", "download",

    # Cache busters & Timestamps
    "_", "cb", "v", "ver", "version", "cache", "nc", "rnd", "t", "timestamp",
}
TRACKING_PARAMS = NON_CONTENT_PARAMS

#: Extensions that are never HTML pages — skipped by the crawler frontier.
NON_PAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".ico", ".bmp", ".tiff",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".dmg", ".exe", ".msi", ".apk",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".ogg", ".wav", ".m4a", ".flv",
    ".css", ".js", ".mjs", ".map", ".json", ".xml", ".rss", ".atom", ".txt",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".woff", ".woff2", ".ttf", ".eot",
}


def normalize_url(url: str, *, strip_tracking: bool = True) -> str:
    """Canonicalise a URL for use as a stable page key.

    Lowercases scheme and host, drops the fragment, removes a trailing slash from non-root paths,
    normalises multiple slashes, strips tracking/state/session parameters and sorts the remaining query.
    """
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()

    # Drop default ports so http://x:80/ and http://x/ are the same page.
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    query = parsed.query
    if query and strip_tracking:
        kept = []
        for k, v in parse_qsl(query, keep_blank_values=True):
            k_lower = k.lower()
            if k_lower in NON_CONTENT_PARAMS:
                continue
            # Strip redundant first page pagination parameter (page=1, p=1, pg=1)
            if k_lower in {"page", "p", "pg"} and v in {"1", "0"}:
                continue
            kept.append((k, v))
        query = urlencode(sorted(kept)) if kept else ""

    return urlunparse((scheme, netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    """SHA-256 of the normalised URL — the stable identity key for a page."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def content_hash(text: str | None) -> str | None:
    """SHA-256 of whitespace-collapsed page text; drives duplicate detection and AI caching."""
    if not text:
        return None
    collapsed = re.sub(r"\s+", " ", text).strip().lower()
    if not collapsed:
        return None
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


def url_path(url: str) -> str:
    """Path (plus query when present) used for GSC/GA4 matching and file→page mapping."""
    parsed = urlparse(normalize_url(url))
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def registrable_domain(url_or_host: str) -> str:
    """Host without a leading ``www.`` — used when comparing sites across providers."""
    host = url_or_host if "://" not in url_or_host else domain_of(url_or_host)
    return host.lower().removeprefix("www.")


def is_http_url(url: str) -> bool:
    return urlparse(url).scheme in {"http", "https"}


def is_same_domain(url: str, base_domain: str) -> bool:
    return registrable_domain(domain_of(url)) == registrable_domain(base_domain)


def has_recursive_path_loop(url: str) -> bool:
    """True if URL path contains repeating segments or embedded hostnames (e.g. /blog/blog/ or /path/www.site.com/)."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    segments = [s.lower() for s in path.split("/") if s]
    if not segments:
        return False

    # Check for embedded hostname inside path
    for seg in segments:
        if "www." in seg or seg.endswith((".com", ".org", ".net", ".io", ".co", ".in")):
            return True

    # Check for segment repetition (e.g., /blog/blog/ or /a/b/a/b/)
    counts = {}
    for i, seg in enumerate(segments):
        counts[seg] = counts.get(seg, 0) + 1
        if counts[seg] > 1 and seg not in {"page", "p", "category", "tag", "index", "en", "us", "uk", "de", "fr"}:
            return True
        if i > 0 and seg == segments[i - 1]:
            return True
    return False


def is_probably_page(url: str) -> bool:
    """False for URLs whose extension or path marks them as an asset, auth, non-page endpoint, or loop."""
    parsed = urlparse(url)
    path = parsed.path.lower()

    if has_recursive_path_loop(url):
        return False

    # Check path segments
    path_segments = [s for s in path.split("/") if s]
    if any(
        seg in (
            "wp-json", "wp-admin", "feed", "cdn-cgi", "api", "xmlrpc.php",
            "login", "signup", "register", "logout", "auth", "cart", "checkout"
        )
        for seg in path_segments
    ):
        return False

    dot = path.rfind(".")
    if dot == -1 or "/" in path[dot:]:
        return True
    return path[dot:] not in NON_PAGE_EXTENSIONS


def is_safe_url(url: str, allow_local: bool = False) -> bool:
    """SSRF guard: reject non-HTTP schemes and any host resolving to a non-public address."""
    if not is_http_url(url):
        return False
    host = domain_of(url)
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} and not allow_local:
        return False
    if allow_local:
        return True
    try:
        for *_, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
    except (socket.gaierror, ValueError):
        return False
    return True


def absolute_url(base: str, href: str) -> str | None:
    """Resolve ``href`` against ``base``, returning ``None`` for non-navigable links."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "sms:")):
        return None

    # Handle malformed hrefs like "www.domain.com/foo" missing scheme
    if href.lower().startswith("www."):
        href = f"https://{href}"

    try:
        url = normalize_url(urljoin(base, href))
    except ValueError:
        return None
    return url if is_http_url(url) else None


def matches_any_pattern(url: str, patterns: list[str] | None) -> bool:
    """Test a URL against glob-ish include/exclude patterns (``*`` is the only wildcard)."""
    if not patterns:
        return False
    path = url_path(url)
    for pattern in patterns:
        regex = "^" + ".*".join(re.escape(part) for part in pattern.split("*")) + "$"
        if re.match(regex, path) or re.match(regex, url):
            return True
    return False

