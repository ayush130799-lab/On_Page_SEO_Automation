"""Groq provider.

Uses the official ``groq`` SDK, which the project already depended on. Groq supports OpenAI-style
JSON mode, so the schema contract is enforced by the API rather than by prompt discipline alone.
"""

from __future__ import annotations

import logging
import time

from .base import LLMError, LLMProvider, LLMRateLimitError, LLMResponse

logger = logging.getLogger(__name__)


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str, timeout: int = 90):
        super().__init__(api_key, model, timeout)
        self._client = None

    def _get_client(self):
        # Created lazily so importing the module never requires a configured key.
        if self._client is None:
            from groq import AsyncGroq

            self._client = AsyncGroq(api_key=self.api_key, timeout=float(self.timeout))
        return self._client

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = True,
    ) -> LLMResponse:
        started = time.monotonic()
        try:
            kwargs = {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = await self._get_client().chat.completions.create(**kwargs)
        except Exception as exc:
            message = str(exc)
            if "429" in message or "rate_limit" in message.lower():
                raise LLMRateLimitError(f"Groq rate limit: {message[:200]}") from exc
            raise LLMError(f"Groq request failed: {type(exc).__name__}") from exc

        usage = getattr(response, "usage", None)
        return LLMResponse(
            content=response.choices[0].message.content or "",
            model=self.model,
            provider=self.name,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
