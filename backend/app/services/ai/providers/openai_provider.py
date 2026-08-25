"""OpenAI provider.

Chat Completions over httpx with ``response_format=json_object``, matching the other providers'
error and timeout handling.
"""

from __future__ import annotations

import logging
import time

import httpx

from .base import LLMError, LLMProvider, LLMRateLimitError, LLMResponse

logger = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(LLMProvider):
    name = "openai"

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = True,
    ) -> LLMResponse:
        body = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
                response = await client.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenAI request failed: {type(exc).__name__}") from exc

        if response.status_code == 429:
            raise LLMRateLimitError("OpenAI rate limit reached.")
        if response.status_code >= 400:
            raise LLMError(f"OpenAI API returned HTTP {response.status_code}.")

        payload = response.json()
        usage = payload.get("usage", {})
        return LLMResponse(
            content=payload["choices"][0]["message"].get("content") or "",
            model=payload.get("model", self.model),
            provider=self.name,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
