"""Heading hierarchy and body-content rules."""

from __future__ import annotations

from ....models.enums import IssueCategory, Severity
from ..registry import fail, ok, rule, warn

THIN_CONTENT_CHARS = 300
SHORT_CONTENT_CHARS = 800


def _h1_texts(page) -> list[str]:
    count = getattr(page, "h1_count", None)
    raw = (getattr(page, "h1", None) or "").strip()
    texts = [t.strip() for t in raw.split(" | ") if t.strip()]
    if count is not None and count > len(texts):
        # Empty <h1> elements contribute to the count but carry no text.
        texts.extend([""] * (count - len(texts)))
    return texts


@rule(
    id="h1",
    check_type="h1",
    category=IssueCategory.HEADINGS,
    weight=10.0,
    title="H1 heading",
    fix_hint="Use exactly one H1 that names the primary topic of the page.",
)
def check_h1(page):
    """One H1 tells crawlers and screen readers what the page is about."""
    headings = _h1_texts(page)

    if not headings:
        return fail(
            "No H1 heading found.",
            severity=Severity.HIGH,
            evidence={"h1_count": 0, "h1": None, "reason": "No <h1> element found in final HTML DOM"},
        )
    if len(headings) > 1:
        return warn(
            f"{len(headings)} H1 headings found; keep one and demote the rest to H2.",
            score=70.0,
            severity=Severity.MEDIUM,
            evidence={"h1_count": len(headings), "headings": [h for h in headings if h][:5]},
        )
    return ok("One H1 heading found.")


@rule(
    id="heading_structure",
    check_type="heading_structure",
    category=IssueCategory.HEADINGS,
    weight=5.0,
    title="Subheading structure",
    fix_hint="Break the content into sections with descriptive H2 and H3 headings.",
)
def check_heading_structure(page):
    """Subheadings make long content scannable and give crawlers section-level context."""
    h2 = getattr(page, "h2_count", 0) or 0
    h3 = getattr(page, "h3_count", 0) or 0

    if h2 == 0 and h3 == 0:
        return warn(
            "No H2 or H3 subheadings were found.",
            score=60.0,
            severity=Severity.LOW,
            evidence={"h2_count": 0, "h3_count": 0, "reason": "No <h2> or <h3> elements found"},
        )
    if h2 == 0 and h3 > 0:
        return warn(
            f"Page uses {h3} H3 headings but no H2, so the hierarchy skips a level.",
            score=80.0,
            severity=Severity.LOW,
            evidence={"h2_count": 0, "h3_count": h3},
        )
    return ok(f"Found {h2} H2 and {h3} H3 headings.")


@rule(
    id="content_length",
    check_type="content",
    category=IssueCategory.CONTENT,
    weight=20.0,
    title="Content depth",
    fix_hint="Expand the page with original, useful content that satisfies the visitor's intent.",
)
def check_content(page):
    """Thin pages rarely rank and dilute site quality signals."""
    content = getattr(page, "content", "") or ""
    length = len(content)
    words = getattr(page, "word_count", None) or len(content.split())

    if length < THIN_CONTENT_CHARS:
        return fail(
            f"Very little readable content ({length} characters, {words} words).",
            score=20.0,
            severity=Severity.HIGH,
            evidence={"characters": length, "words": words},
        )
    if length < SHORT_CONTENT_CHARS:
        return warn(
            f"Readable content is limited ({length} characters, {words} words).",
            score=60.0,
            severity=Severity.MEDIUM,
            evidence={"characters": length, "words": words},
        )
    return ok(f"Readable content length is {length} characters ({words} words).")
