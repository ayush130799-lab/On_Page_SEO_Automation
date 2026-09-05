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
        return fail(
            "Title tag is missing.",
            severity=Severity.HIGH,
            evidence={
                "title": None,
                "length": 0,
                "reason": "No <title> tag found in final parsed HTML",
                "min_allowed": TITLE_MIN,
                "max_allowed": TITLE_MAX,
            },
        )
    if len(title) < TITLE_MIN:
        return warn(
            f"Title is short ({len(title)} characters).",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={
                "title": title,
                "length": len(title),
                "min_allowed": TITLE_MIN,
                "max_allowed": TITLE_MAX,
            },
        )
    if len(title) > TITLE_MAX:
        return warn(
            f"Title is long ({len(title)} characters) and will be truncated in results.",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={
                "title": title,
                "length": len(title),
                "min_allowed": TITLE_MIN,
                "max_allowed": TITLE_MAX,
            },
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
        return fail(
            "Meta description is missing.",
            severity=Severity.HIGH,
            evidence={
                "meta_description": None,
                "length": 0,
                "reason": "No <meta name='description'> tag found in final parsed HTML",
                "min_allowed": META_MIN,
                "max_allowed": META_MAX,
            },
        )
    if len(description) < META_MIN:
        return warn(
            f"Meta description is short ({len(description)} characters).",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={
                "meta_description": description,
                "length": len(description),
                "min_allowed": META_MIN,
                "max_allowed": META_MAX,
            },
        )
    if len(description) > META_MAX:
        return warn(
            f"Meta description is long ({len(description)} characters) and will be truncated.",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={
                "meta_description": description,
                "length": len(description),
                "min_allowed": META_MIN,
                "max_allowed": META_MAX,
            },
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
        "Open Graph metadata is missing.",
        score=70.0,
        severity=Severity.LOW,
        evidence={"has_open_graph": False, "reason": "Missing og:title or og:description tags"},
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
        evidence={"has_viewport": False, "reason": "Missing meta name='viewport' tag"},
    )


@rule(
    id="title_multiple",
    check_type="title_multiple",
    category=IssueCategory.METADATA,
    weight=4.0,
    title="Multiple title tags",
    fix_hint="Keep exactly one <title> in <head>; remove the duplicates.",
)
def check_multiple_titles(page):
    """A second <title> is a conflict search engines resolve arbitrarily."""
    count = getattr(page, "title_count", None)
    if count is None:
        return ok("Title count not available.")
    if count > 1:
        return fail(
            f"{count} <title> tags found — search engines will pick one arbitrarily.",
            score=30.0,
            severity=Severity.HIGH,
            evidence={"title_count": count, "title": getattr(page, "title", None)},
        )
    return ok("Exactly one title tag." if count == 1 else "No title tag found.")


@rule(
    id="meta_description_multiple",
    check_type="meta_description_multiple",
    category=IssueCategory.METADATA,
    weight=2.0,
    title="Multiple meta descriptions",
    fix_hint="Keep exactly one <meta name=\"description\">.",
)
def check_multiple_descriptions(page):
    """Duplicated description tags leave the choice of snippet source to the search engine."""
    count = getattr(page, "meta_description_count", None)
    if count is None:
        return ok("Meta description count not available.")
    if count > 1:
        return warn(
            f"{count} meta description tags found.",
            score=50.0,
            severity=Severity.MEDIUM,
            evidence={"meta_description_count": count},
        )
    return ok("Exactly one meta description." if count == 1 else "No meta description found.")
