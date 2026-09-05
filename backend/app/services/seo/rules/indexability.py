"""Rules covering whether a page can be crawled, indexed and resolved to one canonical URL."""

from __future__ import annotations

from ....models.enums import IssueCategory, Severity
from ....utils.url_utils import normalize_url
from ..registry import fail, ok, rule, warn

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@rule(
    id="http_status",
    check_type="http_status",
    category=IssueCategory.INDEXABILITY,
    weight=10.0,
    title="HTTP response status",
    fix_hint="Fix the server or application response so the URL returns HTTP 200.",
)
def check_http_status(page):
    """A page that does not return 200 cannot rank, whatever else is right about it."""
    status = page.status_code or 0

    if status == 200:
        return ok("Page returned HTTP 200.")

    if status in REDIRECT_STATUSES:
        return warn(
            f"Page redirects with HTTP {status}.",
            score=70.0,
            severity=Severity.MEDIUM,
            recommendation="Link directly to the destination URL so users and crawlers skip the hop.",
            evidence={"status_code": status},
        )

    if status == 0:
        return fail(
            "Page could not be fetched at all.",
            severity=Severity.CRITICAL,
            recommendation="Check DNS, TLS and server availability for this URL.",
            evidence={"status_code": 0, "error": getattr(page, "crawl_error", None)},
        )

    return fail(
        f"Page returned HTTP {status}.",
        severity=Severity.CRITICAL,
        evidence={"status_code": status},
    )


@rule(
    id="robots_directive",
    check_type="robots",
    category=IssueCategory.INDEXABILITY,
    weight=10.0,
    title="Robots meta directive",
    fix_hint="Remove the blocking directive if this page should appear in search results.",
)
def check_robots(page):
    """`noindex` silently removes a page from search - the highest-impact single tag on a page.

    Directives are tokenised rather than substring-matched: `max-image-preview:none` contains the
    word "none" but is not `robots: none`, and an `X-Robots-Tag` scoped to another crawler says
    nothing about how this page will be treated.
    """
    from ..robots_directives import describe, resolve

    directives = resolve(
        getattr(page, "robots_directive", None) or getattr(page, "meta_robots", None),
        getattr(page, "x_robots_tag", None),
    )
    evidence = directives.as_evidence()
    summary = describe(directives)

    if "none" in directives.directives:
        return fail(
            "Page carries 'robots: none', which means noindex plus nofollow.",
            score=20.0,
            severity=Severity.CRITICAL,
            evidence=evidence,
        )
    if "noindex" in directives.directives:
        source = directives.sources.get("noindex", "meta")
        where = "the X-Robots-Tag header" if source == "header" else "the robots meta tag"
        return fail(
            f"Page carries a 'noindex' directive in {where} and will not be indexed.",
            score=20.0,
            severity=Severity.CRITICAL,
            evidence=evidence,
        )
    if "nofollow" in directives.directives:
        return warn(
            "Page carries a 'nofollow' directive, so its links pass no signal.",
            score=60.0,
            severity=Severity.HIGH,
            evidence=evidence,
        )
    if directives.directives or directives.values:
        return ok(f"Robots directives present and permissive: {summary}.")
    return ok("No restrictive robots directive found.")


@rule(
    id="canonical_multiple",
    check_type="canonical_multiple",
    category=IssueCategory.INDEXABILITY,
    weight=6.0,
    title="Multiple canonical tags",
    fix_hint="Remove duplicate <link rel=\"canonical\"> tags and keep exactly one per page.",
)
def check_multiple_canonical(page):
    """Multiple canonicals create a conflict that search engines resolve arbitrarily."""
    count = getattr(page, "canonical_count", None)
    if count is None:
        # Field not present (old crawl data) — skip rather than false-positive.
        return ok("Canonical count not available.")
    if count > 1:
        return fail(
            f"{count} canonical tags found — search engines will pick one arbitrarily.",
            score=20.0,
            severity=Severity.CRITICAL,
            evidence={"canonical_count": count, "canonical_url": getattr(page, "canonical_url", None)},
        )
    return ok(f"Exactly {count} canonical tag found." if count == 1 else "No canonical tag found.")


@rule(
    id="canonical_present",
    check_type="canonical",
    category=IssueCategory.INDEXABILITY,
    weight=5.0,
    title="Canonical URL",
    fix_hint="Add a <link rel=\"canonical\"> element pointing at the preferred URL for this page.",
)
def check_canonical(page):
    """Without a canonical, parameterised and duplicated variants compete with each other."""
    canonical = getattr(page, "canonical_url", None)
    count = getattr(page, "canonical_count", 1 if canonical else 0)
    # If multiple canonical rule already flagged this, pass here to avoid double-penalising.
    if count > 1:
        return ok("Canonical presence check skipped (multiple canonical tags detected).")
    if canonical:
        return ok("Canonical URL is present.")
    return warn(
        "Canonical URL is missing.",
        score=50.0,
        severity=Severity.HIGH,
        evidence={"canonical_url": None, "reason": "No <link rel='canonical'> tag found in final parsed HTML"},
    )


@rule(
    id="canonical_target",
    check_type="canonical_target",
    category=IssueCategory.INDEXABILITY,
    weight=4.0,
    title="Canonical target",
    fix_hint="Point the canonical at this page's own URL unless it is a deliberate duplicate.",
)
def check_canonical_target(page):
    """A canonical pointing elsewhere de-indexes this URL — usually a templating mistake."""
    canonical = getattr(page, "canonical_url", None)
    if not canonical:
        return ok("No canonical to validate.")

    own = normalize_url(getattr(page, "final_url", None) or page.url)
    target = normalize_url(canonical)
    if target == own:
        return ok("Canonical points at this page.")

    return warn(
        f"Canonical points to a different URL ({target}), so this page defers to it.",
        score=40.0,
        severity=Severity.HIGH,
        evidence={"canonical": target, "page_url": own},
    )


@rule(
    id="redirect_chain",
    check_type="redirect_chain",
    category=IssueCategory.INDEXABILITY,
    weight=4.0,
    title="Redirect chain",
    fix_hint="Update the source link or redirect rule to point straight at the final destination.",
)
def check_redirect_chain(page):
    """Each hop loses crawl budget and a little link equity; loops lose the page entirely."""
    chain = list(getattr(page, "redirect_chain", None) or [])
    if not chain:
        return ok("URL resolves without redirects.")

    final = normalize_url(getattr(page, "final_url", None) or page.url)
    normalised_chain = [normalize_url(hop) for hop in chain]

    # A single hop that normalises to the destination is a trailing-slash or scheme redirect.
    # It costs nothing and reporting it would bury real findings under noise on most sites.
    if len(normalised_chain) == 1 and normalised_chain[0] == final:
        return ok("URL redirects only to its canonical form (trailing slash or scheme).")

    # A genuine loop revisits a URL: either a hop repeats, or the destination is a hop the chain
    # already passed through before its final step.
    revisits_a_hop = len(set(normalised_chain)) < len(normalised_chain)
    returns_to_start = len(normalised_chain) >= 2 and final in normalised_chain
    if revisits_a_hop or returns_to_start:
        return fail(
            "Redirect loop detected — the chain returns to a URL it already visited.",
            severity=Severity.CRITICAL,
            evidence={"chain": chain + [final]},
        )

    if len(chain) >= 3:
        return fail(
            f"Redirect chain is {len(chain)} hops long before reaching {final}.",
            score=30.0,
            severity=Severity.HIGH,
            evidence={"chain": chain + [final], "hops": len(chain)},
        )
    if len(chain) == 2:
        return warn(
            f"Redirect chain has 2 hops before reaching {final}.",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={"chain": chain + [final], "hops": 2},
        )
    return warn(
        f"URL redirects once to {final}.",
        score=85.0,
        severity=Severity.LOW,
        evidence={"chain": chain + [final], "hops": 1},
    )
