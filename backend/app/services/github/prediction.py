"""Pre-Deployment SEO Prediction — roadmap §8.2 and §8.3.

Turns a list of :class:`~app.services.github.diff_analyzer.DetectedChange` into the dual
assessment §8.2 asks for: a positive-impact view with its own confidence, a negative-impact view
with its own confidence, an overall risk level, and the exact PR-comment format from §8.3.

This is where Step 4 actually depends on Steps 1-2 existing, per the roadmap's own sequencing
note ("depends on the scoring/intent engines existing to judge whether a diff helps or hurts"):
a title or H1 rewrite is ambiguous from text alone (the diff analyzer reports it "neutral"), but
if the affected page has a known primary keyword from Step 2's intent profile, losing that keyword
from the H1 is unambiguously bad and gaining it is unambiguously good. :func:`refine_with_keywords`
does that cross-reference before scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .diff_analyzer import DetectedChange

#: A finding's weighted contribution is scaled by how many total signals exist, so one file with
#: five signals does not automatically read as five times riskier than a focused one-line fix —
#: what matters is the mix of directions, not the raw count.
_RISK_THRESHOLDS = {
    "critical": 2.2,
    "high": 1.2,
    "medium": 0.5,
}


@dataclass(slots=True)
class DeploymentPrediction:
    expected_impact: str  # positive | negative | mixed | neutral
    positive_confidence: float
    negative_confidence: float
    risk_level: str  # low | medium | high | critical
    positive_findings: list[str] = field(default_factory=list)
    negative_findings: list[str] = field(default_factory=list)
    recommendation: str = ""
    suggested_changes: list[str] = field(default_factory=list)


def refine_with_keywords(
    changes: list[DetectedChange], keywords_by_url: dict[str, list[str]]
) -> list[DetectedChange]:
    """Resolve a "neutral, text changed" title/H1 finding into positive/negative when the
    affected page has a known primary/secondary keyword to check it against.

    ``keywords_by_url`` maps a page's URL to its primary + secondary keywords (lower-cased),
    typically read from ``PageIntentProfile`` for that page.
    """
    refined: list[DetectedChange] = []
    for change in changes:
        if change.change_type not in ("title", "h1") or change.direction != "neutral":
            refined.append(change)
            continue

        keywords = keywords_by_url.get(change.affected_url or "", [])
        if not keywords:
            refined.append(change)
            continue

        before = (change.before_value or "").lower()
        after = (change.after_value or "").lower()
        had_keyword = any(kw in before for kw in keywords)
        has_keyword = any(kw in after for kw in keywords)

        if had_keyword and not has_keyword:
            refined.append(_replace(
                change, direction="negative", weight=change.weight * 1.6,
                description=(
                    f"{change.description} The target keyword present in the previous "
                    f"{change.change_type} is no longer present — this weakens topical "
                    f"relevance for the keyword this page was ranking for."
                ),
            ))
        elif has_keyword and not had_keyword:
            refined.append(_replace(
                change, direction="positive", weight=change.weight * 1.3,
                description=(
                    f"{change.description} The new {change.change_type} now includes the "
                    f"page's target keyword, which the previous version did not."
                ),
            ))
        else:
            refined.append(change)
    return refined


def _replace(change: DetectedChange, **overrides) -> DetectedChange:
    from dataclasses import replace
    return replace(change, **overrides)


def predict_deployment_impact(changes: list[DetectedChange]) -> DeploymentPrediction:
    """The §8.2 dual assessment from a set of detected changes."""
    if not changes:
        return DeploymentPrediction(
            expected_impact="neutral",
            positive_confidence=0.0,
            negative_confidence=0.0,
            risk_level="low",
            recommendation="No SEO-relevant changes were detected in this diff.",
        )

    positive_weight = sum(c.weight for c in changes if c.direction == "positive")
    negative_weight = sum(c.weight for c in changes if c.direction == "negative")
    total_weight = sum(c.weight for c in changes) or 1.0

    positive_findings = [c.description for c in changes if c.direction == "positive"]
    negative_findings = [c.description for c in changes if c.direction == "negative"]

    # Confidence: how much of the *total signal* points each way, damped so a single weak signal
    # never reads as near-certain. §9.2's rule applies here too — this is a ranked expectation,
    # never a guarantee.
    positive_confidence = round(min(0.95, (positive_weight / total_weight) * 0.9), 2)
    negative_confidence = round(min(0.95, (negative_weight / total_weight) * 0.9), 2)

    if positive_weight > 0 and negative_weight == 0:
        expected_impact = "positive"
    elif negative_weight > 0 and positive_weight == 0:
        expected_impact = "negative"
    elif positive_weight > 0 and negative_weight > 0:
        expected_impact = "mixed"
    else:
        expected_impact = "neutral"

    # Risk tracks the negative side specifically, not net sentiment — a page that gained three
    # good things and lost indexability is still critical, not "mixed and therefore fine".
    risk_score = negative_weight
    # A newly-added noindex is always at least critical, regardless of its weight relative to
    # other signals in the same diff: it removes the page from search entirely.
    added_noindex = any(
        c.direction == "negative" and c.change_type == "robots"
        and "noindex directive was added" in (c.description or "").lower()
        for c in changes
    )

    if added_noindex or risk_score >= _RISK_THRESHOLDS["critical"]:
        risk_level = "critical"
    elif risk_score >= _RISK_THRESHOLDS["high"]:
        risk_level = "high"
    elif risk_score >= _RISK_THRESHOLDS["medium"]:
        risk_level = "medium"
    else:
        risk_level = "low"

    recommendation, suggested = _recommendation_and_suggestions(changes, risk_level)

    return DeploymentPrediction(
        expected_impact=expected_impact,
        positive_confidence=positive_confidence,
        negative_confidence=negative_confidence,
        risk_level=risk_level,
        positive_findings=positive_findings,
        negative_findings=negative_findings,
        recommendation=recommendation,
        suggested_changes=suggested,
    )


def _recommendation_and_suggestions(
    changes: list[DetectedChange], risk_level: str
) -> tuple[str, list[str]]:
    negatives = [c for c in changes if c.direction == "negative"]

    if risk_level in ("critical", "high"):
        recommendation = "DO NOT DEPLOY WITHOUT REVISION"
    elif risk_level == "medium":
        recommendation = "Review before merging — some findings may reduce SEO performance."
    else:
        recommendation = (
            "No blocking SEO concerns detected."
            if not negatives
            else "Safe to merge; minor findings noted below."
        )

    suggestions: list[str] = []
    for change in negatives:
        if change.change_type == "robots":
            suggestions.append("Remove the added noindex directive if this page should be indexed.")
        elif change.change_type == "canonical":
            suggestions.append("Restore the canonical tag pointing at this page's preferred URL.")
        elif change.change_type == "h1":
            suggestions.append("Keep the target keyword in the H1 heading.")
        elif change.change_type == "title":
            suggestions.append("Keep the target keyword in the page title.")
        elif change.change_type == "schema":
            suggestions.append("Restore the removed structured data block.")
        elif change.change_type == "content_length":
            suggestions.append("Verify the content reduction was intentional; restore relevant sections if not.")
        elif change.change_type == "internal_links":
            suggestions.append("Preserve internal links removed from this page.")
    # De-duplicate while keeping order — several files can trigger the same suggestion.
    seen: set[str] = set()
    unique = [s for s in suggestions if not (s in seen or seen.add(s))]
    return recommendation, unique


def format_pr_comment(
    pr_number: int, changes: list[DetectedChange], prediction: DeploymentPrediction,
    *, affected_urls: list[str] | None = None,
) -> str:
    """The exact §8.3 layout: Overall Impact, Risk, Affected URL, Changes detected, Expected SEO
    impact, Recommendation, Suggested changes."""
    urls = affected_urls or sorted({c.affected_url for c in changes if c.affected_url})
    url_line = urls[0] if len(urls) == 1 else (
        f"{len(urls)} URLs" if urls else "No URL could be resolved from the changed files"
    )

    lines = [
        f"## PR #{pr_number} — SEO Impact Analysis",
        "",
        f"**Overall Impact:** {prediction.expected_impact.upper()}  ",
        f"**Risk:** {prediction.risk_level.upper()}  ",
        f"**Affected URL{'s' if len(urls) != 1 else ''}:** {url_line}",
        "",
    ]

    if urls and len(urls) > 1:
        lines.append("<details><summary>All affected URLs</summary>\n")
        lines.extend(f"- {u}" for u in urls[:50])
        lines.append("\n</details>\n")

    lines.append("**Changes detected:**")
    if changes:
        label = {"negative": "removed/regressed", "positive": "improved", "neutral": "changed"}
        for c in changes:
            lines.append(f"- ({label[c.direction]}) {c.description}")
    else:
        lines.append("- No SEO-relevant changes detected.")
    lines.append("")

    lines.append("**Expected SEO impact:**")
    if prediction.positive_findings:
        lines.append(f"- Positive (confidence {prediction.positive_confidence * 100:.0f}%):")
        lines.extend(f"  - {f}" for f in prediction.positive_findings)
    if prediction.negative_findings:
        lines.append(f"- Negative (confidence {prediction.negative_confidence * 100:.0f}%):")
        lines.extend(f"  - {f}" for f in prediction.negative_findings)
    if not prediction.positive_findings and not prediction.negative_findings:
        lines.append("- No measurable impact expected.")
    lines.append("")

    lines.append(f"**Recommendation:** {prediction.recommendation}")

    if prediction.suggested_changes:
        lines.append("")
        lines.append("**Suggested changes:**")
        lines.extend(f"{i}. {s}" for i, s in enumerate(prediction.suggested_changes, 1))

    lines.append("")
    lines.append(
        "_Automated SEO impact prediction. Impact and confidence are ranked expectations "
        "based on detected changes, not a guarantee of ranking or traffic outcomes._"
    )

    return "\n".join(lines)
