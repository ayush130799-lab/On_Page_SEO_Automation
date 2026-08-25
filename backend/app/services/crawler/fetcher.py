"""HTTP fetching with per-host rate limiting, retries and redirect-chain capture."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

from ...utils.url_utils import normalize_url

logger = logging.getLogger(__name__)

RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)
#: Transient server-side statuses worth a retry. 429 is included so a burst backs off rather than
#: being recorded as a permanent failure.
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    """Everything one HTTP fetch produced, including how it got there."""

    url: str
    final_url: str
    status_code: int
    html: str = ""
    content_type: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    content_bytes: int = 0
    error: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    @property
    def redirected(self) -> bool:
        return bool(self.redirect_chain)


class HostRateLimiter:
    """Token bucket per host, so one slow origin cannot be hammered by every worker."""

    def __init__(self, requests_per_second: float):
        self.rate = max(0.1, requests_per_second)
        self._next_slot: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, host: str) -> None:
        if self.rate <= 0:
            return
        interval = 1.0 / self.rate
        async with self._locks[host]:
            now = time.monotonic()
            wait_for = self._next_slot[host] - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._next_slot[host] = now + interval


async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    limiter: HostRateLimiter | None = None,
    max_retries: int = 3,
    timeout: float | None = None,
) -> FetchResult:
    """Fetch one URL, never raising: transport problems become a FetchResult with an error."""
    host = httpx.URL(url).host or ""
    last_error: str | None = None
    started = time.monotonic()

    for attempt in range(1, max(1, max_retries) + 1):
        if limiter is not None:
            await limiter.acquire(host)
        try:
            response = await client.get(url, timeout=timeout) if timeout else await client.get(url)
        except RETRYABLE_EXCEPTIONS as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                await asyncio.sleep(min(8.0, 0.25 * (2 ** (attempt - 1))))
                continue
            break
        except Exception as exc:  # non-retryable transport problem (bad URL, TLS refusal, …)
            last_error = f"{type(exc).__name__}: {exc}"
            break

        if response.status_code in RETRYABLE_STATUSES and attempt < max_retries:
            retry_after = _retry_after_seconds(response)
            await asyncio.sleep(retry_after or min(8.0, 0.5 * (2 ** (attempt - 1))))
            last_error = f"HTTP {response.status_code}"
            continue

        content_type = response.headers.get("content-type", "")
        html = response.text if "html" in content_type.lower() else ""
        return FetchResult(
            url=url,
            final_url=normalize_url(str(response.url)),
            status_code=response.status_code,
            html=html,
            content_type=content_type,
            redirect_chain=[normalize_url(str(r.url)) for r in response.history],
            elapsed_ms=int((time.monotonic() - started) * 1000),
            content_bytes=len(response.content),
            attempts=attempt,
        )

    # Every attempt failed at the transport layer. Status 0 marks "never reached".
    return FetchResult(
        url=url,
        final_url=url,
        status_code=0,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        error=last_error or "Request failed.",
        attempts=max(1, max_retries),
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Honour a numeric ``Retry-After`` header, capped so one header cannot stall a crawl."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return min(30.0, float(raw))
    except ValueError:
        return None
