"""Google Gemini provider.

Uses the official Google Gemini REST API (v1beta), supporting models like
gemini-2.5-flash, gemini-1.5-flash, gemini-2.5-pro, and gemini-1.5-pro.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .base import LLMError, LLMProvider, LLMRateLimitError, LLMResponse

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(LLMProvider):
    name = "gemini"

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
        endpoint = f"{GEMINI_API_BASE}/models/{self.model}:generateContent"

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"System Instructions:\n{system_prompt}\n\nUser Task:\n{user_prompt}"}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        try:
            async with httpx.AsyncClient(timeout=float(self.timeout)) as client:
                response = await client.post(endpoint, headers=headers, json=payload)

            if response.status_code == 429:
                raise LLMRateLimitError(f"Gemini API rate limit: {response.text[:200]}")
            if response.status_code != 200:
                raise LLMError(f"Gemini API returned status {response.status_code}: {response.text[:200]}")

            data = response.json()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"Gemini request failed: {type(exc).__name__}: {exc}") from exc

        try:
            candidates = data.get("candidates", [])
            if not candidates:
                raise LLMError("Gemini returned no candidates.")

            parts = candidates[0].get("content", {}).get("parts", [])
            text = parts[0].get("text", "") if parts else ""
        except (IndexError, KeyError, TypeError) as exc:
            raise LLMError(f"Failed to parse Gemini response structure: {exc}") from exc

        usage = data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount")
        completion_tokens = usage.get("candidatesTokenCount")

        return LLMResponse(
            content=text,
            model=self.model,
            provider=self.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
            raw=data,
        )
