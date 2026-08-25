"""Structured data, URL hygiene and internationalisation rules."""

from __future__ import annotations

from urllib.parse import urlparse

from ....models.enums import IssueCategory, Severity
from ..registry import fail, ok, rule, warn

MAX_URL_LENGTH = 100
MAX_PATH_DEPTH = 5


@rule(
    id="structured_data",
    check_type="structured_data",
    category=IssueCategory.STRUCTURED_DATA,
    weight=1.5,
    title="Schema.org structured data",
    fix_hint="Add JSON-LD matching the page type (Article, Product, FAQPage, BreadcrumbList…).",
)
def check_structured_data(page):
    """Structured data unlocks rich results, which lift CTR without changing rank."""
    if getattr(page, "structured_data_invalid", False):
        return fail(
            "A JSON-LD block on this page is not valid JSON and will be ignored by search engines.",
            score=30.0,
            severity=Severity.MEDIUM,
        )
    if getattr(page, "has_structured_data", False):
        types = getattr(page, "structured_data_types", None) or []
        label = ", ".join(types[:5]) if types else "present"
        return ok(f"Structured data detected ({label}).")
    return warn(
        "No Schema.org structured data detected.", score=70.0, severity=Severity.LOW
    )


@rule(
    id="url_structure",
    check_type="url_structure",
    category=IssueCategory.INDEXABILITY,
    weight=2.0,
    title="URL structure",
    fix_hint="Use short, lowercase, hyphenated paths without spaces or session parameters.",
)
def check_url_structure(page):
    """Clean URLs are easier to share, easier to read in results and less prone to duplication."""
    url = getattr(page, "final_url", None) or getattr(page, "url", "") or ""
    parsed = urlparse(url)
    problems: list[str] = []

    if len(url) > MAX_URL_LENGTH:
        problems.append(f"{len(url)} characters long")
    if " " in url or "%20" in url:
        problems.append("contains spaces")
    if any(ch.isupper() for ch in parsed.path):
        problems.append("contains uppercase characters")
    if parsed.path.count("/") > MAX_PATH_DEPTH:
        problems.append(f"{parsed.path.count('/')} levels deep")
    if "_" in parsed.path:
        problems.append("uses underscores instead of hyphens")

    if not problems:
        return ok("URL structure is clean.")
    return warn(
        "URL structure is non-optimal: " + ", ".join(problems) + ".",
        score=60.0,
        severity=Severity.LOW,
        evidence={"url": url, "problems": problems},
    )


@rule(
    id="hreflang",
    check_type="hreflang",
    category=IssueCategory.INTERNATIONAL,
    weight=1.0,
    title="Language and hreflang",
    fix_hint='Set <html lang="…"> and, on multilingual sites, reciprocal hreflang alternates.',
)
def check_hreflang(page):
    """Declaring language prevents the wrong locale being served in international results."""
    lang = (getattr(page, "lang", None) or "").strip()
    alternates = getattr(page, "hreflang", None) or []

    if not lang and not alternates:
        return warn(
            "No lang attribute on <html> and no hreflang alternates declared.",
            score=60.0,
            severity=Severity.LOW,
        )
    if not lang:
        return warn(
            "hreflang alternates are declared but <html> has no lang attribute.",
            score=70.0,
            severity=Severity.LOW,
            evidence={"alternates": len(alternates)},
        )

    if alternates:
        malformed = [a for a in alternates if not a.get("href") or not a.get("lang")]
        if malformed:
            return warn(
                f"{len(malformed)} hreflang alternate(s) are missing a language or href.",
                score=60.0,
                severity=Severity.LOW,
                evidence={"malformed": malformed[:5]},
            )
        has_self = any(
            (a.get("lang") or "").lower().startswith(lang.lower().split("-")[0])
            for a in alternates
        )
        if not has_self:
            return warn(
                "hreflang alternates do not include a self-referencing entry for this page's "
                f"language ({lang}).",
                score=75.0,
                severity=Severity.LOW,
                evidence={"lang": lang, "alternates": [a.get("lang") for a in alternates][:10]},
            )
        return ok(f"Language '{lang}' declared with {len(alternates)} hreflang alternate(s).")

    return ok(f"Language '{lang}' declared.")
