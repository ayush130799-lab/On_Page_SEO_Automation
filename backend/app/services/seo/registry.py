"""The SEO rule registry.

Adding a rule is a single decorated function:

    @rule(id="my_check", check_type="my_check", category=IssueCategory.CONTENT,
          weight=3.0, title="Something is wrong")
    def check_something(page: PageSignals) -> RuleOutcome | None:
        if page.word_count < 50:
            return fail("Only %d words." % page.word_count, severity=Severity.HIGH)
        return None

Nothing else changes: scoring, persistence, the dashboard and the AI prompt all read from the
registry, so a new rule flows through the whole system automatically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from ...models.enums import IssueCategory, Severity

logger = logging.getLogger(__name__)

PASS = "pass"
WARNING = "warning"
FAIL = "fail"
#: The rule did not run because the page was never successfully retrieved. Distinct from
#: "pass" so that a skipped check cannot inflate the score.
SKIPPED = "skipped"


class PageSignals(Protocol):
    """The attribute surface a rule may rely on (satisfied by ExtractedPage and ORM Page)."""

    url: str
    status_code: int | None
    title: str | None
    meta_description: str | None


@dataclass(slots=True)
class RuleOutcome:
    """What a rule concluded about one page."""

    status: str
    score: float
    details: str
    severity: str | None = None
    recommendation: str | None = None
    evidence: dict[str, Any] | None = None


@dataclass(slots=True)
class RuleResult:
    """A :class:`RuleOutcome` bound to the rule that produced it."""

    rule_id: str
    check_type: str
    category: str
    title: str
    status: str
    score: float
    details: str
    severity: str | None = None
    recommendation: str | None = None
    evidence: dict[str, Any] | None = None

    @property
    def is_issue(self) -> bool:
        return self.status not in (PASS, SKIPPED) and self.severity is not None

    @property
    def was_evaluated(self) -> bool:
        """False when the check could not run, so it must not contribute to the score."""
        return self.status != SKIPPED


@dataclass
class Rule:
    """Registry entry describing one check."""

    id: str
    check_type: str
    category: str
    title: str
    weight: float
    description: str
    fix_hint: str
    func: Callable[[Any], RuleOutcome | None]
    #: Site-wide rules run after every page is known (duplicates, orphans, …).
    site_wide: bool = False
    enabled: bool = True

    def evaluate(self, page: Any) -> RuleResult:
        """Run the rule, converting any failure into a neutral pass rather than breaking the audit."""
        try:
            outcome = self.func(page)
        except Exception as exc:
            logger.warning("Rule '%s' raised on %s: %s", self.id, getattr(page, "url", "?"), exc)
            outcome = None

        if outcome is None:
            outcome = RuleOutcome(PASS, 100.0, "No problem detected.")

        return RuleResult(
            rule_id=self.id,
            check_type=self.check_type,
            category=self.category,
            title=self.title,
            status=outcome.status,
            score=outcome.score,
            details=outcome.details,
            severity=outcome.severity,
            recommendation=outcome.recommendation or self.fix_hint,
            evidence=outcome.evidence,
        )


class RuleRegistry:
    """Ordered collection of rules, keyed by id."""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}

    def register(self, rule_obj: Rule) -> Rule:
        if rule_obj.id in self._rules:
            raise ValueError(f"A rule with id '{rule_obj.id}' is already registered.")
        self._rules[rule_obj.id] = rule_obj
        return rule_obj

    def all(self, *, include_disabled: bool = False) -> list[Rule]:
        return [r for r in self._rules.values() if include_disabled or r.enabled]

    def page_rules(self) -> list[Rule]:
        return [r for r in self.all() if not r.site_wide]

    def site_rules(self) -> list[Rule]:
        return [r for r in self.all() if r.site_wide]

    def get(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def weights(self) -> dict[str, float]:
        """Default weight per check type, derived from the registered rules."""
        weights: dict[str, float] = {}
        for rule_obj in self.all():
            weights[rule_obj.check_type] = weights.get(rule_obj.check_type, 0.0) + rule_obj.weight
        return weights

    def __len__(self) -> int:
        return len(self._rules)

    def __iter__(self) -> Iterable[Rule]:
        return iter(self._rules.values())


registry = RuleRegistry()


def rule(
    *,
    id: str,
    check_type: str,
    category: IssueCategory | str,
    weight: float,
    title: str,
    description: str = "",
    fix_hint: str = "",
    site_wide: bool = False,
):
    """Decorator registering a function as an SEO rule."""

    def decorator(func: Callable[[Any], RuleOutcome | None]):
        registry.register(
            Rule(
                id=id,
                check_type=check_type,
                category=str(category),
                title=title,
                weight=weight,
                description=description or (func.__doc__ or "").strip(),
                fix_hint=fix_hint,
                func=func,
                site_wide=site_wide,
            )
        )
        return func

    return decorator


# ── Outcome helpers, so rule bodies stay one-liners ─────────────────────────


def ok(details: str = "No problem detected.", score: float = 100.0) -> RuleOutcome:
    return RuleOutcome(PASS, score, details)


def warn(
    details: str,
    *,
    score: float = 60.0,
    severity: str = Severity.MEDIUM,
    recommendation: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> RuleOutcome:
    return RuleOutcome(WARNING, score, details, severity, recommendation, evidence)


def fail(
    details: str,
    *,
    score: float = 0.0,
    severity: str = Severity.HIGH,
    recommendation: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> RuleOutcome:
    return RuleOutcome(FAIL, score, details, severity, recommendation, evidence)
