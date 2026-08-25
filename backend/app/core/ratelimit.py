"""Fixed-window rate limiting.

Backed by Redis when available so limits hold across API replicas, with an in-process fallback so
local development and the test suite work without Redis running.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from fastapi import Request

from ..config import settings
from .errors import RateLimitError

logger = logging.getLogger(__name__)

_local_buckets: dict[str, list[float]] = defaultdict(list)
_local_lock = threading.Lock()

_redis_client = None
_redis_checked = False


def _get_redis():
    """Return a Redis client, or ``None`` if unavailable (checked once per process)."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    try:
        import redis  # imported lazily so the API starts without redis installed

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        _redis_client = client
    except Exception as exc:
        logger.info("Rate limiter falling back to in-process counters (%s).", exc)
        _redis_client = None
    return _redis_client


def _client_key(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


def _hit_local(key: str, limit: int, window: int) -> bool:
    now = time.time()
    with _local_lock:
        bucket = _local_buckets[key]
        cutoff = now - window
        # Drop expired entries, then test the window.
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


def _hit_redis(client, key: str, limit: int, window: int) -> bool:
    bucket = f"ratelimit:{key}:{int(time.time() // window)}"
    try:
        pipe = client.pipeline()
        pipe.incr(bucket, 1)
        pipe.expire(bucket, window + 1)
        count = pipe.execute()[0]
        return int(count) <= limit
    except Exception as exc:  # pragma: no cover - network failure must not block requests
        logger.warning("Redis rate-limit check failed, allowing request: %s", exc)
        return True


def check_rate_limit(request: Request, scope: str, limit: int, window_seconds: int = 60) -> None:
    """Raise :class:`RateLimitError` when the caller exceeds ``limit`` per ``window_seconds``."""
    if not settings.rate_limit_enabled:
        return
    key = f"{scope}:{_client_key(request)}"
    client = _get_redis()
    allowed = (
        _hit_redis(client, key, limit, window_seconds)
        if client is not None
        else _hit_local(key, limit, window_seconds)
    )
    if not allowed:
        raise RateLimitError(
            f"Too many requests. Limit is {limit} per {window_seconds} seconds.",
            {"scope": scope, "retry_after_seconds": window_seconds},
        )


def auth_rate_limit(request: Request) -> None:
    """Dependency for authentication endpoints (credential-stuffing protection)."""
    check_rate_limit(request, "auth", settings.rate_limit_auth_per_minute)


def default_rate_limit(request: Request) -> None:
    """Dependency for expensive endpoints such as crawl triggers."""
    check_rate_limit(request, "default", settings.rate_limit_default_per_minute)


def reset_rate_limits() -> None:
    """Clear in-process counters (used by tests)."""
    with _local_lock:
        _local_buckets.clear()
