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
from .gemini_provider import GeminiProvider
from .groq_provider import GroqProvider
from .openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def provider_credentials(name: str) -> tuple[str, str]:
    """Return ``(api_key, model)`` for a provider name."""
    return {
        "gemini": (settings.gemini_api_key, settings.gemini_model),
        "groq": (settings.groq_api_key, settings.groq_model),
        "anthropic": (settings.anthropic_api_key, settings.anthropic_model),
        "openai": (settings.openai_api_key, settings.openai_model),
    }.get(name, ("", ""))


def get_provider(name: str | None = None) -> LLMProvider | None:
    """Build the configured provider, or ``None`` when no key is available."""
    requested = (name or settings.llm_provider or "gemini").strip().lower()

    # If the requested provider has no API key, pick any provider that DOES have a key.
    api_key, model = provider_credentials(requested)
    if not api_key:
        available = available_providers()
        if available:
            requested = available[0]
            api_key, model = provider_credentials(requested)
        else:
            logger.info("No LLM provider has an API key configured.")
            return None

    provider_class = PROVIDERS.get(requested)
    if provider_class is None:
        logger.warning("Unknown LLM provider '%s'; falling back to gemini.", requested)
        requested, provider_class = "gemini", GeminiProvider
        api_key, model = provider_credentials(requested)
        if not api_key:
            return None

    return provider_class(api_key=api_key, model=model, timeout=settings.ai_timeout_seconds)


def available_providers() -> list[str]:
    """Provider names that currently have a key configured."""
    return [name for name in PROVIDERS if provider_credentials(name)[0]]


def get_active_providers() -> list[LLMProvider]:
    """Return initialized instances for ALL providers that currently have an API key configured.

    Enables multi-provider load balancing and fallback retries across Groq, Gemini, OpenAI, etc.
    """
    active: list[LLMProvider] = []
    for name in available_providers():
        provider = get_provider(name)
        if provider is not None and provider not in active:
            active.append(provider)

    if not active:
        fallback = get_provider()
        if fallback is not None:
            active.append(fallback)

    return active


__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "GroqProvider",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponse",
    "OpenAIProvider",
    "PROVIDERS",
    "available_providers",
    "extract_json",
    "get_active_providers",
    "get_provider",
    "provider_credentials",
]
