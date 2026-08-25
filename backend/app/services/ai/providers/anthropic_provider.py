"""Anthropic provider.

Talks to the Messages API over httpx rather than pulling in another SDK — the surface used is one
endpoint, and it keeps the retry and timeout behaviour identical across providers.

Anthropic has no JSON mode, so JSON is elicited by prefilling the assistant turn with an opening
brace. That is the documented technique and it is far more reliable than asking in prose.
"""

from __future__ import annotations

import logging
import time

import httpx

from .base import LLMError, LLMProvider, LLMRateLimitError, LLMResponse

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = True,
    ) -> LLMResponse:
        messages = [{"role": "user", "content": user_prompt}]
        if json_mode:
            # Prefilling an opening brace forces the model to continue a JSON object.
            messages.append({"role": "assistant", "content": "{"})

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
                response = await client.post(
                    API_URL,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": API_VERSION,
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "system": system_prompt,
                        "messages": messages,
                    },
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"Anthropic request failed: {type(exc).__name__}") from exc

        if response.status_code == 429:
            raise LLMRateLimitError("Anthropic rate limit reached.")
        if response.status_code >= 400:
            raise LLMError(f"Anthropic API returned HTTP {response.status_code}.")

        payload = response.json()
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        # Restore the brace that was used to prefill the assistant turn.
        if json_mode and not text.lstrip().startswith("{"):
            text = "{" + text

        usage = payload.get("usage", {})
        return LLMResponse(
            content=text,
            model=payload.get("model", self.model),
            provider=self.name,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
