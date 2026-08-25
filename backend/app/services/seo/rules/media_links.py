"""Image accessibility, internal linking and outbound-link rules."""

from __future__ import annotations

from ....models.enums import IssueCategory, Severity
from ..registry import fail, ok, rule, warn


@rule(
    id="image_alt",
    check_type="image_alt",
    category=IssueCategory.IMAGES,
    weight=10.0,
    title="Image alt text",
    fix_hint="Describe informative images in alt text; leave alt empty only for decoration.",
)
def check_image_alt(page):
    """Alt text is both an accessibility requirement and the only text signal an image carries."""
    total = getattr(page, "image_count", 0) or 0
    missing = getattr(page, "missing_alt_count", 0) or 0

    if total == 0:
        return ok("No images found.")
    if missing == 0:
        return ok(f"All {total} images have alt text.")

    coverage = round(100 * (total - missing) / total)
    severity = Severity.HIGH if missing / total > 0.5 else Severity.MEDIUM
    return fail(
        f"{missing} of {total} images are missing alt text.",
        score=coverage,
        severity=severity,
        evidence={"images": total, "missing_alt": missing, "coverage_percent": coverage},
    )


@rule(
    id="image_dimensions",
    check_type="image_dimensions",
    category=IssueCategory.IMAGES,
    weight=1.0,
    title="Image dimensions",
    fix_hint="Set width and height on <img> so the browser reserves space and avoids layout shift.",
)
def check_image_dimensions(page):
    """Missing intrinsic dimensions cause cumulative layout shift, a Core Web Vitals metric."""
    total = getattr(page, "image_count", 0) or 0
    undimensioned = getattr(page, "images_without_dimensions", 0) or 0

    if total == 0 or undimensioned == 0:
        return ok("All images declare width and height.")
    if undimensioned / total < 0.25:
        return ok(f"{undimensioned} of {total} images omit dimensions.", score=90.0)

    return warn(
        f"{undimensioned} of {total} images do not declare width and height.",
        score=70.0,
        severity=Severity.LOW,
        evidence={"images": total, "without_dimensions": undimensioned},
    )


@rule(
    id="internal_links",
    check_type="internal_links",
    category=IssueCategory.LINKS,
    weight=5.0,
    title="Outgoing internal links",
    fix_hint="Link to related pages so crawlers can discover them and users can navigate onward.",
)
def check_internal_links(page):
    """A page with no outgoing internal links is a dead end for crawlers and readers."""
    count = getattr(page, "internal_link_count", None)
    if count is None:
        count = len(getattr(page, "internal_links", []) or [])

    if count == 0:
        return warn(
            "No internal links were found on this page.", score=40.0, severity=Severity.HIGH
        )
    if count < 3:
        return warn(
            f"Only {count} internal link(s) found.",
            score=75.0,
            severity=Severity.LOW,
            evidence={"internal_links": count},
        )
    return ok(f"{count} internal links found.")


@rule(
    id="broken_links",
    check_type="broken_links",
    category=IssueCategory.LINKS,
    weight=5.0,
    title="Broken internal links",
    fix_hint="Update or remove links that point at 4xx/5xx URLs.",
)
def check_broken_links(page):
    """Broken links waste crawl budget and are a direct user-experience failure."""
    broken = getattr(page, "broken_link_count", 0) or 0
    if broken == 0:
        return ok("No broken internal links detected.")

    severity = Severity.HIGH if broken > 2 else Severity.MEDIUM
    return fail(
        f"{broken} internal link(s) point at URLs returning HTTP 4xx/5xx.",
        score=30.0,
        severity=severity,
        evidence={"broken_links": broken},
    )


@rule(
    id="orphan_page",
    check_type="orphan_page",
    category=IssueCategory.LINKS,
    weight=2.0,
    title="Inbound internal links",
    fix_hint="Link to this page from relevant navigation, hubs or related content.",
    site_wide=True,
)
def check_orphan_page(page):
    """A page nothing links to is discoverable only via the sitemap and accrues no internal equity."""
    inbound = getattr(page, "inbound_internal_links", 0) or 0
    url = getattr(page, "final_url", None) or getattr(page, "url", "")

    # The homepage is legitimately reached without inbound internal links.
    from urllib.parse import urlparse

    if (urlparse(url).path or "/") in {"/", ""}:
        return ok("Homepage does not require inbound internal links.")

    if inbound == 0:
        return warn(
            "No other crawled page links to this URL (orphan page).",
            score=40.0,
            severity=Severity.MEDIUM,
            evidence={"inbound_internal_links": 0},
        )
    if inbound == 1:
        return ok("One internal link points here.", score=85.0)
    return ok(f"{inbound} internal links point here.")


@rule(
    id="external_links",
    check_type="external_links",
    category=IssueCategory.LINKS,
    weight=1.0,
    title="Outbound external links",
    fix_hint="Cite authoritative sources where it helps the reader; mark paid links rel=sponsored.",
)
def check_external_links(page):
    """An excess of nofollowed outbound links usually indicates injected or scraped content."""
    external = getattr(page, "external_link_count", None)
    if external is None:
        external = len(getattr(page, "external_links", []) or [])
    nofollow = getattr(page, "nofollow_link_count", 0) or 0

    if external > 100:
        return warn(
            f"{external} outbound external links — unusually high for a single page.",
            score=60.0,
            severity=Severity.LOW,
            evidence={"external_links": external, "nofollow_links": nofollow},
        )
    return ok(f"{external} outbound external link(s).")
