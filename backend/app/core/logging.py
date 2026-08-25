"""Logging configuration with mandatory secret redaction.

Every log record — message, args and exception text — is passed through a redaction filter before
it reaches a handler, so tokens and API keys cannot leak into log files even when a third-party
library logs a request URL or an error body verbatim.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..config import settings

REDACTED = "***REDACTED***"

# Patterns are deliberately broad: it is better to over-redact a log line than to leak a token.
_PATTERNS: list[re.Pattern[str]] = [
    # key="value" / key: value / key=value  for anything that smells like a secret
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"client[_-]?secret|secret[_-]?key|password|passwd|authorization|auth[_-]?token|"
        r"webhook[_-]?secret|private[_-]?key|bearer)\b"
        r"(\s*[:=]\s*[\"']?|\"\s*:\s*\"|'\s*:\s*')"
        r"([^\s,;&\"'}\)\]]+)"
    ),
    # Authorization: Bearer <token>
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]{12,}=*)"),
    # ?key=... in query strings (Semrush and similar key-in-URL APIs)
    re.compile(r"(?i)([?&](?:key|api_key|access_token|token)=)([^&\s]+)"),
    # Google OAuth / service-account style tokens
    re.compile(r"\bya29\.[A-Za-z0-9\-._~+/]+=*"),
    # Fernet / JWT-looking blobs
    re.compile(r"\beyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\b"),
    # GitHub tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # Anthropic / OpenAI / Groq style keys
    re.compile(r"\b(?:sk|gsk)-[A-Za-z0-9\-_]{16,}\b"),
]


def redact(text: str) -> str:
    """Replace anything that looks like a credential with :data:`REDACTED`."""
    if not text:
        return text
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        elif pattern.groups == 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Strip secrets from the message, the formatting args and the exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                record.args = self._redact_args(record.args)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # pragma: no cover - logging must never raise
            pass
        return True

    @staticmethod
    def _redact_args(args: Any) -> Any:
        if isinstance(args, dict):
            return {k: redact(v) if isinstance(v, str) else v for k, v in args.items()}
        if isinstance(args, tuple):
            return tuple(redact(a) if isinstance(a, str) else a for a in args)
        return args


def configure_logging(level: int | None = None) -> None:
    """Install the root logging configuration with redaction attached to every handler."""
    resolved = level if level is not None else (logging.DEBUG if settings.debug else logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved)

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s :: %(message)s")
        )
        root.addHandler(handler)

    redactor = RedactingFilter()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(redactor)

    # Third-party loggers that are chatty at INFO.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
