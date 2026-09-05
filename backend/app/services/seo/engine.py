"""The SEO audit engine: run the rule registry over crawled pages and score the results."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ...models.enums import Severity
from . import rules  # noqa: F401  (importing registers every rule)
from .registry import SKIPPED, RuleResult, registry
from .scoring import (
    calculate_score,
    determine_category,
    determine_highest_severity,
    determine_priority_band,
    resolve_weights,
    severity_counts,
)

logger = logging.getLogger(__name__)

MIN_WORDS_FOR_DUPLICATE_CHECK = 50


@dataclass
class PageAuditResult:
    """The complete audit outcome for one page."""

    url: str
    seo_score: float
    category: str
    highest_severity: str
    priority_band: str
    results: list[RuleResult] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)

    @property
    def issues(self) -> list[RuleResult]:
        return [r for r in self.results if r.is_issue]

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def counts(self) -> dict[str, int]:
        return severity_counts(self.results)

    @property
    def has_critical(self) -> bool:
        return self.highest_severity == Severity.CRITICAL

    def checks_payload(self) -> list[dict[str, Any]]:
        """Compact per-check record persisted on the SEOAudit row."""
        return [
            {
                "rule_id": r.rule_id,
                "check": r.check_type,
                "status": r.status,
                "score": r.score,
                "severity": r.severity,
                "details": r.details,
            }
            for r in self.results
        ]


def annotate_site(pages: Sequence[Any]) -> None:
    """Compute cross-page signals and attach them to each page.

    Site-wide rules (duplicate title/description/content) then read these attributes exactly like
    any other page attribute, which keeps a single rule execution path.
    """
    by_title: dict[str, list[Any]] = defaultdict(list)
    by_meta: dict[str, list[Any]] = defaultdict(list)
    by_content: dict[str, list[Any]] = defaultdict(list)

    for page in pages:
        title = (getattr(page, "title", None) or "").strip().lower()
        if title:
            by_title[title].append(page)

        meta = (getattr(page, "meta_description", None) or "").strip().lower()
        if meta:
            by_meta[meta].append(page)

        digest = getattr(page, "content_hash", None)
        words = getattr(page, "word_count", 0) or 0
        # Very short pages (empty states, redirect stubs) collide trivially; ignore them.
        if digest and words >= MIN_WORDS_FOR_DUPLICATE_CHECK:
            by_content[digest].append(page)

    def _url(page: Any) -> str:
        return getattr(page, "final_url", None) or getattr(page, "url", "")

    for page in pages:
        page.duplicate_title_urls = []
        page.duplicate_meta_urls = []
        page.duplicate_content_urls = []

    for group in (by_title, by_meta, by_content):
        attribute = {
            id(by_title): "duplicate_title_urls",
            id(by_meta): "duplicate_meta_urls",
            id(by_content): "duplicate_content_urls",
        }[id(group)]
        for members in group.values():
            if len(members) < 2:
                continue
            for page in members:
                setattr(
                    page,
                    attribute,
                    [_url(other) for other in members if other is not page],
                )


def audit_page(page: Any, weights: dict[str, float] | None = None) -> PageAuditResult:
    """Run every registered rule against one page and score the outcome."""
    resolved = weights if weights is not None else resolve_weights()
    results = []
    
    # `or 200` here would turn status 0 - a request that never completed - into a healthy page,
    # and the whole rule set would then run against an empty document, manufacturing a dozen
    # "missing title/H1/content" issues for a site that was merely unreachable.
    raw_status = getattr(page, "status_code", None)
    status_code = 200 if raw_status is None else raw_status
    is_200 = status_code == 200

    # A page whose fetch failed carries no DOM at all; only the status rule can say anything
    # truthful about it.
    usable = getattr(page, "is_usable", True)
    evaluable = is_200 and usable

    for rule_obj in registry.all():
        # Non-200 pages (404s, 500s, 3xx redirects) and failed fetches are only evaluated by the
        # status and redirect rules - every other check would be describing a document we never
        # successfully retrieved.
        if not evaluable and rule_obj.id not in {"http_status", "redirect_chain"}:
            results.append(
                RuleResult(
                    rule_id=rule_obj.id,
                    check_type=rule_obj.check_type,
                    category=rule_obj.category,
                    title=rule_obj.title,
                    status=SKIPPED,
                    score=0.0,
                    details=(
                        f"Check skipped: HTTP {status_code}"
                        + ("" if usable else " and no document was retrieved")
                        + "."
                    ),
                )
            )
        else:
            results.append(rule_obj.evaluate(page))

    score = calculate_score(results, resolved)
    highest = determine_highest_severity(results)

    return PageAuditResult(
        url=getattr(page, "final_url", None) or getattr(page, "url", ""),
        seo_score=score,
        category=determine_category(score),
        highest_severity=highest,
        priority_band=determine_priority_band(highest),
        results=results,
        weights=resolved,
    )



def audit_site(
    pages: Sequence[Any], weights: dict[str, float] | None = None
) -> list[PageAuditResult]:
    """Annotate the site, then audit every page.

    This is the entry point the pipeline uses: duplicate detection needs the full page set, so a
    per-page loop alone would silently under-report.
    """
    if not pages:
        return []

    annotate_site(pages)
    resolved = weights if weights is not None else resolve_weights()
    audits = [audit_page(page, resolved) for page in pages]

    logger.info(
        "Audited %d pages with %d rules; %d pages carry a CRITICAL issue.",
        len(audits),
        len(registry),
        sum(1 for a in audits if a.has_critical),
    )
    return audits


def rule_catalogue() -> list[dict[str, Any]]:
    """Describe every registered rule — powers the settings UI and API documentation."""
    return [
        {
            "id": r.id,
            "check_type": r.check_type,
            "category": r.category,
            "title": r.title,
            "weight": r.weight,
            "description": r.description,
            "fix_hint": r.fix_hint,
            "site_wide": r.site_wide,
        }
        for r in registry.all(include_disabled=True)
    ]


def aggregate_scores(audits: Iterable[PageAuditResult]) -> dict[str, Any]:
    """Roll per-page audits up into the numbers a crawl run and website summary display."""
    audits = list(audits)
    if not audits:
        return {
            "page_count": 0,
            "average_seo_score": None,
            "total_issues": 0,
            "critical_issues": 0,
            "by_category": {},
            "by_severity": {},
        }

    by_category: dict[str, int] = defaultdict(int)
    by_severity: dict[str, int] = defaultdict(int)
    total_issues = 0
    critical_issues = 0

    for audit in audits:
        by_category[audit.category] += 1
        by_severity[audit.highest_severity] += 1
        total_issues += audit.issue_count
        critical_issues += audit.counts[Severity.CRITICAL]

    return {
        "page_count": len(audits),
        "average_seo_score": round(sum(a.seo_score for a in audits) / len(audits), 1),
        "total_issues": total_issues,
        "critical_issues": critical_issues,
        "by_category": dict(by_category),
        "by_severity": dict(by_severity),
    }
