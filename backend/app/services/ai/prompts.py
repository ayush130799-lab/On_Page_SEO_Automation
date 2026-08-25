"""Prompt construction.

The prompt carries the *actual* data the platform holds — the page's extracted content, the
deterministic rule findings, and its real Search Console / GA4 / Semrush numbers — rather than
asking the model to speculate. That is what keeps recommendations specific and checkable, and it is
why the rule engine runs first: the model is asked to explain and fix findings, not to rediscover
them.
"""

from __future__ import annotations

import json
from typing import Any

from ...config import settings

SYSTEM_PROMPT = """You are a senior technical SEO consultant reviewing one page of a website that \
your engineering team maintains.

You are given: the page's extracted content, the findings of a deterministic SEO rule engine, and \
the page's real analytics and search performance. The rule engine has already established the \
facts — do not second-guess whether a title or heading exists. Your job is to explain what those \
findings mean for this specific page, and to write fixes a developer can apply today.

Rules you must follow:
- Ground every statement in the supplied data. Never invent metrics, keywords, competitors or \
facts that are not present.
- Write suggested titles, descriptions and headings as final copy, not as instructions.
- Respect the documented length limits: titles 30-60 characters, meta descriptions 70-160.
- Prioritise by business consequence, using the supplied traffic and conversion figures. A fix on \
a page with real traffic matters more than the same fix on a page with none.
- If the page is genuinely in good shape, say so and return few or no findings rather than \
manufacturing work.
- Implementation guidance should name the concrete artefact to edit (template, component, CMS \
field, schema block) as far as the data allows.

Respond with a single JSON object and nothing else. It must have exactly these keys:
  summary                 string
  search_intent           string: informational | commercial | transactional | navigational
  content_quality_score   number 0-100
  topic_coverage_score    number 0-100
  findings                array of objects:
                            issue, explanation, why_it_matters, recommended_fix,
                            implementation, expected_impact,
                            priority (critical|high|medium|low),
                            effort (trivial|small|medium|large),
                            confidence (0-1)
  suggested_changes       array of objects: field, current, suggested, rationale
  expected_impact         string
  priority                critical | high | medium | low
  confidence              number 0-1
  implementation_notes    string
"""

REPAIR_PROMPT = """Your previous response could not be parsed against the required schema.

Validation errors:
{errors}

Return the corrected JSON object only. No prose, no code fences, no explanation."""


def _format_metrics(metrics: dict[str, Any] | None, window_days: int) -> str:
    if not metrics or not any(metrics.get(k) for k in ("users", "clicks", "impressions")):
        return "No analytics or Search Console data is available for this page."

    lines = [f"Performance over the last {window_days} days:"]
    if metrics.get("users") or metrics.get("sessions"):
        lines.append(
            f"  Analytics: {metrics.get('users', 0):,} users, "
            f"{metrics.get('sessions', 0):,} sessions, "
            f"{metrics.get('conversions', 0):,.0f} conversions, "
            f"{metrics.get('revenue', 0):,.2f} revenue"
        )
        if metrics.get("engagement_rate") is not None:
            lines.append(f"  Engagement rate: {metrics['engagement_rate']:.1%}")
    if metrics.get("clicks") or metrics.get("impressions"):
        ctr = metrics.get("ctr")
        lines.append(
            f"  Search Console: {metrics.get('clicks', 0):,} clicks, "
            f"{metrics.get('impressions', 0):,} impressions"
            + (f", CTR {ctr:.2%}" if ctr is not None else "")
            + (
                f", average position {metrics['position']}"
                if metrics.get("position") is not None
                else ""
            )
        )
    if metrics.get("organic_keywords"):
        lines.append(
            f"  Semrush: {metrics['organic_keywords']} ranking keywords, "
            f"{metrics.get('striking_distance_keywords', 0)} in striking distance "
            f"(positions 4-20), {metrics.get('backlinks', 0)} backlinks"
        )
    return "\n".join(lines)


def _format_queries(queries: list[dict[str, Any]] | None) -> str:
    if not queries:
        return ""
    lines = ["Top search queries already reaching this page:"]
    for entry in queries[:10]:
        lines.append(
            f"  \"{entry.get('query')}\" — {entry.get('clicks', 0)} clicks, "
            f"{entry.get('impressions', 0)} impressions, position {entry.get('position')}"
        )
    return "\n".join(lines)


def _format_keywords(keywords: list[dict[str, Any]] | None) -> str:
    if not keywords:
        return ""
    lines = ["Keywords this page ranks for (Semrush):"]
    for entry in keywords[:10]:
        lines.append(
            f"  \"{entry.get('keyword')}\" — position {entry.get('position')}, "
            f"volume {entry.get('volume', 0):,}, difficulty {entry.get('difficulty')}"
        )
    return "\n".join(lines)


def _format_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "The rule engine found no issues on this page."
    lines = ["Deterministic rule-engine findings (these are established facts):"]
    for issue in issues:
        lines.append(
            f"  [{issue.get('severity')}] {issue.get('rule_id')}: {issue.get('description')}"
        )
        if issue.get("evidence"):
            lines.append(f"      evidence: {json.dumps(issue['evidence'])[:300]}")
    return "\n".join(lines)


def build_user_prompt(
    page: Any,
    issues: list[dict[str, Any]],
    metrics: dict[str, Any] | None = None,
    *,
    priority_score: float | None = None,
    priority_band: str | None = None,
    queries: list[dict[str, Any]] | None = None,
    keywords: list[dict[str, Any]] | None = None,
    window_days: int | None = None,
) -> str:
    """Assemble the per-page prompt from real platform data."""
    window = window_days or settings.priority_metric_window_days
    content = (getattr(page, "content", "") or "")[: settings.ai_max_content_length]

    sections = [
        f"URL: {getattr(page, 'url', '')}",
        f"HTTP status: {getattr(page, 'status_code', 'unknown')}",
        "",
        "Current on-page values:",
        f"  Title: {getattr(page, 'title', None) or '(missing)'}",
        f"  Meta description: {getattr(page, 'meta_description', None) or '(missing)'}",
        f"  H1: {getattr(page, 'h1', None) or '(missing)'}",
        f"  H2 count: {getattr(page, 'h2_count', 0)}   H3 count: {getattr(page, 'h3_count', 0)}",
        f"  Canonical: {getattr(page, 'canonical_url', None) or '(none)'}",
        f"  Robots: {getattr(page, 'robots_directive', None) or '(none)'}",
        f"  Language: {getattr(page, 'lang', None) or '(not declared)'}",
        f"  Word count: {getattr(page, 'word_count', 0)}",
        f"  Images: {getattr(page, 'image_count', 0)} "
        f"({getattr(page, 'missing_alt_count', 0)} without alt text)",
        f"  Internal links out: {getattr(page, 'internal_link_count', 0)}   "
        f"in: {getattr(page, 'inbound_internal_links', 0)}",
        f"  Structured data: {', '.join(getattr(page, 'structured_data_types', None) or []) or 'none'}",
        "",
        f"SEO health score: {getattr(page, 'seo_score', 'n/a')}/100 "
        f"({getattr(page, 'seo_category', 'n/a')}, "
        f"worst severity {getattr(page, 'highest_severity', 'n/a')})",
    ]

    if priority_score is not None:
        sections.append(
            f"Business priority score: {priority_score}/100 (band {priority_band or 'n/a'})"
        )

    sections += ["", _format_issues(issues), "", _format_metrics(metrics, window)]

    for extra in (_format_queries(queries), _format_keywords(keywords)):
        if extra:
            sections += ["", extra]

    sections += [
        "",
        "Page text content:",
        content or "(no readable text was extracted)",
    ]

    return "\n".join(sections)
