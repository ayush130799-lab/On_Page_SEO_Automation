"""The structured contract for an AI recommendation.

Every provider must return JSON matching :class:`PageRecommendation`. Validating with Pydantic
before persistence means a malformed or hallucinated shape is caught at the boundary rather than
surfacing as a broken dashboard, and it lets the repair retry send the model a precise description
of what was wrong.

Fields map one-to-one onto what the specification requires per finding: what the issue is, why it
matters, how to fix it, the concrete SEO change to make, the expected impact, and developer-facing
implementation guidance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Priority = Literal["critical", "high", "medium", "low"]
Effort = Literal["trivial", "small", "medium", "large"]


class SuggestedChange(BaseModel):
    """A concrete, copy-pasteable edit."""

    field: str = Field(
        description="What to change: title, meta_description, h1, content, schema, internal_links…"
    )
    current: str | None = Field(default=None, description="The current value, if there is one.")
    suggested: str = Field(description="The exact replacement value.")
    rationale: str | None = None


class Finding(BaseModel):
    """One problem the model identified, with its full explanation and fix."""

    issue: str = Field(description="Short name of the problem.")
    explanation: str = Field(description="What is wrong, in plain language.")
    why_it_matters: str = Field(description="The concrete SEO or business consequence.")
    recommended_fix: str = Field(description="What to do about it.")
    implementation: str | None = Field(
        default=None, description="Developer-facing guidance: where and how to make the change."
    )
    expected_impact: str | None = None
    priority: Priority = "medium"
    effort: Effort = "small"
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)

    @field_validator("priority", mode="before")
    @classmethod
    def normalise_priority(cls, value):
        text = str(value or "medium").strip().lower()
        # Models routinely answer "P0"/"urgent"/"HIGH"; accept the obvious synonyms.
        return {
            "p0": "critical", "p1": "high", "p2": "medium", "p3": "low",
            "urgent": "critical", "severe": "critical", "moderate": "medium", "minor": "low",
        }.get(text, text if text in {"critical", "high", "medium", "low"} else "medium")

    @field_validator("effort", mode="before")
    @classmethod
    def normalise_effort(cls, value):
        text = str(value or "small").strip().lower()
        return {
            "xs": "trivial", "s": "small", "m": "medium", "l": "large", "xl": "large",
            "quick": "trivial", "easy": "trivial", "hard": "large",
        }.get(text, text if text in {"trivial", "small", "medium", "large"} else "small")

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_confidence(cls, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.7
        # Some models answer on a 0-100 scale despite the instruction.
        if number > 1.0:
            number = number / 100.0
        return min(1.0, max(0.0, number))


class PageRecommendation(BaseModel):
    """The complete response for one page."""

    summary: str = Field(description="One or two sentences on the page's overall situation.")
    search_intent: str | None = Field(
        default=None, description="informational | commercial | transactional | navigational"
    )
    content_quality_score: float = Field(default=0.0, ge=0, le=100)
    topic_coverage_score: float = Field(default=0.0, ge=0, le=100)
    findings: list[Finding] = Field(default_factory=list)
    suggested_changes: list[SuggestedChange] = Field(default_factory=list)
    expected_impact: str | None = Field(
        default=None, description="What fixing everything here should achieve."
    )
    priority: Priority = "medium"
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    implementation_notes: str | None = Field(
        default=None, description="Cross-cutting guidance for the developer making these changes."
    )

    _normalise_priority = field_validator("priority", mode="before")(
        Finding.normalise_priority.__func__
    )
    _normalise_confidence = field_validator("confidence", mode="before")(
        Finding.normalise_confidence.__func__
    )

    @field_validator("content_quality_score", "topic_coverage_score", mode="before")
    @classmethod
    def clamp_score(cls, value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        # A 0-1 answer to a 0-100 question is a common model slip.
        if 0 < number <= 1:
            number *= 100
        return min(100.0, max(0.0, number))

    @property
    def suggested_title(self) -> str | None:
        return self._change_for("title")

    @property
    def suggested_meta_description(self) -> str | None:
        return self._change_for("meta_description", "meta description", "description")

    def _change_for(self, *names: str) -> str | None:
        wanted = {n.lower().replace(" ", "_") for n in names}
        for change in self.suggested_changes:
            if change.field.lower().replace(" ", "_") in wanted:
                return change.suggested
        return None


#: JSON Schema handed to providers that support structured output natively.
RESPONSE_JSON_SCHEMA = PageRecommendation.model_json_schema()
