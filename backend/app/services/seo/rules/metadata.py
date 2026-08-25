"""Title, meta description, social metadata and mobile viewport rules.

Length thresholds match the original MVP so historical scores stay comparable.
"""

from __future__ import annotations

from ....models.enums import IssueCategory, Severity
from ..registry import fail, ok, rule, warn

TITLE_MIN = 30
TITLE_MAX = 60
META_MIN = 70
META_MAX = 160


@rule(
    id="title",
    check_type="title",
    category=IssueCategory.METADATA,
    weight=8.0,
    title="Title tag",
    fix_hint="Write a unique 30-60 character title that states the page topic, keyword first.",
)
def check_title(page):
    """The title is the single strongest on-page relevance signal and the SERP headline."""
    title = (page.title or "").strip()

    if not title:
        return fail("Title tag is missing.", severity=Severity.HIGH)
    if len(title) < TITLE_MIN:
        return warn(
            f"Title is short ({len(title)} characters).",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={"title": title, "length": len(title)},
        )
    if len(title) > TITLE_MAX:
        return warn(
            f"Title is long ({len(title)} characters) and will be truncated in results.",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={"title": title, "length": len(title)},
        )
    return ok(f"Title length is {len(title)} characters.")


@rule(
    id="meta_description",
    check_type="meta_description",
    category=IssueCategory.METADATA,
    weight=7.0,
    title="Meta description",
    fix_hint="Write a unique 70-160 character description summarising the page's value.",
)
def check_meta_description(page):
    """Not a ranking factor, but it drives click-through on impressions already earned."""
    description = (page.meta_description or "").strip()

    if not description:
        return fail("Meta description is missing.", severity=Severity.HIGH)
    if len(description) < META_MIN:
        return warn(
            f"Meta description is short ({len(description)} characters).",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={"length": len(description)},
        )
    if len(description) > META_MAX:
        return warn(
            f"Meta description is long ({len(description)} characters) and will be truncated.",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={"length": len(description)},
        )
    return ok(f"Meta description length is {len(description)} characters.")


@rule(
    id="open_graph",
    check_type="open_graph",
    category=IssueCategory.METADATA,
    weight=1.5,
    title="Open Graph metadata",
    fix_hint="Add og:title, og:description and og:image so shared links render a rich preview.",
)
def check_open_graph(page):
    """Controls how the page looks when shared, which affects referral and social traffic."""
    if getattr(page, "has_open_graph", False):
        return ok("Open Graph metadata detected.")
    return warn(
        "Open Graph metadata is missing.", score=70.0, severity=Severity.LOW
    )


@rule(
    id="viewport",
    check_type="viewport",
    category=IssueCategory.METADATA,
    weight=1.0,
    title="Mobile viewport",
    fix_hint='Add <meta name="viewport" content="width=device-width, initial-scale=1">.',
)
def check_viewport(page):
    """Indexing is mobile-first; a missing viewport makes the page render at desktop width."""
    if getattr(page, "has_viewport", False):
        return ok("Viewport meta tag is present.")
    return warn(
        "Viewport meta tag is missing, so the page will not adapt to mobile screens.",
        score=50.0,
        severity=Severity.MEDIUM,
    )
