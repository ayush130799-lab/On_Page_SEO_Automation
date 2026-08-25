"""LLM provider registry.

``LLM_PROVIDER`` selects the backend; each provider reads its own key and model from settings, so
switching vendors never touches application code.
"""

from __future__ import annotations

import logging

from ....config import settings
from .anthropic_provider import AnthropicProvider
from .base import (
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    extract_json,
)
from .groq_provider import GroqProvider
from .openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, type[LLMProvider]] = {
    "groq": GroqProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def provider_credentials(name: str) -> tuple[str, str]:
    """Return ``(api_key, model)`` for a provider name."""
    return {
        "groq": (settings.groq_api_key, settings.groq_model),
        "anthropic": (settings.anthropic_api_key, settings.anthropic_model),
        "openai": (settings.openai_api_key, settings.openai_model),
    }.get(name, ("", ""))


def get_provider(name: str | None = None) -> LLMProvider | None:
    """Build the configured provider, or ``None`` when no key is available.

    Returning ``None`` rather than raising is deliberate: a deployment with no LLM key must still
    crawl, audit, score and prioritise. AI is an enrichment layer, not a dependency.
    """
    requested = (name or settings.llm_provider or "groq").strip().lower()
    provider_class = PROVIDERS.get(requested)
    if provider_class is None:
        logger.warning("Unknown LLM provider '%s'; falling back to groq.", requested)
        requested, provider_class = "groq", GroqProvider

    api_key, model = provider_credentials(requested)
    if not api_key:
        logger.info("LLM provider '%s' has no API key configured.", requested)
        return None

    return provider_class(api_key=api_key, model=model, timeout=settings.ai_timeout_seconds)


def available_providers() -> list[str]:
    """Provider names that currently have a key configured."""
    return [name for name in PROVIDERS if provider_credentials(name)[0]]


__all__ = [
    "AnthropicProvider",
    "GroqProvider",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponse",
    "OpenAIProvider",
    "PROVIDERS",
    "available_providers",
    "extract_json",
    "get_provider",
    "provider_credentials",
]
