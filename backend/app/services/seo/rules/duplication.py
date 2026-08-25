"""Site-wide duplication rules.

These read fields that :func:`app.services.seo.engine.annotate_site` computes across the whole
crawl, so they run through the same registry as every other rule rather than needing a parallel
code path.
"""

from __future__ import annotations

from ....models.enums import IssueCategory, Severity
from ..registry import ok, rule, warn


@rule(
    id="duplicate_title",
    check_type="duplicate_title",
    category=IssueCategory.METADATA,
    weight=3.0,
    title="Duplicate title",
    fix_hint="Give every page a title that describes what makes that specific URL different.",
    site_wide=True,
)
def check_duplicate_title(page):
    """Duplicate titles make pages compete with each other for the same query."""
    duplicates = getattr(page, "duplicate_title_urls", None) or []
    if not duplicates:
        return ok("Title is unique across the crawled site.")

    total = len(duplicates) + 1
    return warn(
        f"This title is used on {total} pages.",
        score=40.0,
        severity=Severity.MEDIUM,
        evidence={"duplicate_count": total, "examples": duplicates[:5]},
    )


@rule(
    id="duplicate_meta_description",
    check_type="duplicate_meta_description",
    category=IssueCategory.METADATA,
    weight=2.0,
    title="Duplicate meta description",
    fix_hint="Write a distinct description per page, or let search engines generate one.",
    site_wide=True,
)
def check_duplicate_meta(page):
    """A boilerplate description repeated site-wide adds nothing to any result."""
    duplicates = getattr(page, "duplicate_meta_urls", None) or []
    if not duplicates:
        return ok("Meta description is unique across the crawled site.")

    total = len(duplicates) + 1
    return warn(
        f"This meta description is used on {total} pages.",
        score=50.0,
        severity=Severity.LOW,
        evidence={"duplicate_count": total, "examples": duplicates[:5]},
    )


@rule(
    id="duplicate_content",
    check_type="duplicate_content",
    category=IssueCategory.CONTENT,
    weight=4.0,
    title="Duplicate content",
    fix_hint="Consolidate the duplicates, or canonicalise them to a single preferred URL.",
    site_wide=True,
)
def check_duplicate_content(page):
    """Byte-identical body text across URLs splits ranking signals between them."""
    duplicates = getattr(page, "duplicate_content_urls", None) or []
    if not duplicates:
        return ok("Page content is unique across the crawled site.")

    total = len(duplicates) + 1
    # A correct canonical is the sanctioned fix, so it downgrades rather than clears the finding.
    canonical = getattr(page, "canonical_url", None)
    own = getattr(page, "final_url", None) or getattr(page, "url", "")
    if canonical and canonical != own:
        return warn(
            f"Content is identical to {total - 1} other page(s), but a canonical is declared.",
            score=75.0,
            severity=Severity.LOW,
            evidence={"duplicate_count": total, "examples": duplicates[:5], "canonical": canonical},
        )

    return warn(
        f"Content is byte-identical to {total - 1} other page(s) with no canonical to resolve it.",
        score=30.0,
        severity=Severity.HIGH,
        evidence={"duplicate_count": total, "examples": duplicates[:5]},
    )
