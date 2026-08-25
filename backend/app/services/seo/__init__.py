"""Deterministic on-page SEO rule engine and health scoring."""

from .engine import (
    PageAuditResult,
    aggregate_scores,
    annotate_site,
    audit_page,
    audit_site,
    rule_catalogue,
)
from .registry import Rule, RuleOutcome, RuleResult, fail, ok, registry, rule, warn
from .scoring import (
    calculate_score,
    determine_category,
    determine_highest_severity,
    determine_priority_band,
    resolve_weights,
    severity_counts,
)

__all__ = [
    "PageAuditResult",
    "Rule",
    "RuleOutcome",
    "RuleResult",
    "aggregate_scores",
    "annotate_site",
    "audit_page",
    "audit_site",
    "calculate_score",
    "determine_category",
    "determine_highest_severity",
    "determine_priority_band",
    "fail",
    "ok",
    "registry",
    "resolve_weights",
    "rule",
    "rule_catalogue",
    "severity_counts",
    "warn",
]
