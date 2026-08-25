"""The LLM provider abstraction.

A provider's only job is: take a system prompt and a user prompt, return parsed JSON plus token
usage. Everything above it — prompt construction, schema validation, the repair retry, the
selection gate, persistence — is provider-agnostic, so swapping vendors is a configuration change.
"""

from __future__ import annotations

import abc
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """A provider call failed in a way the caller should handle, not crash on."""


class LLMRateLimitError(LLMError):
    """The provider rate-limited us; the caller should back off and retry."""


@dataclass
class LLMResponse:
    """One completed model call."""

    content: str
    model: str
    provider: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        """Parse the response body as JSON, tolerating the wrappers models add."""
        return extract_json(self.content)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Even in JSON mode, models occasionally wrap output in a code fence or prepend a sentence.
    Rather than fail the page, recover the object.
    """
    if not text or not text.strip():
        raise LLMError("The model returned an empty response.")

    candidate = text.strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Last resort: the outermost braces.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise LLMError("The model response did not contain a JSON object.")


class LLMProvider(abc.ABC):
    """Base class for every model backend."""

    name: str = "base"

    def __init__(self, api_key: str, model: str, timeout: int = 90):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        """Whether this provider is configured well enough to be called."""
        return bool(self.api_key and self.model)

    @abc.abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = True,
    ) -> LLMResponse:
        """Run one completion and return the raw response."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} model={self.model!r}>"
