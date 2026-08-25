"""GitHub webhook signature verification.

GitHub signs every delivery with HMAC-SHA256 over the raw request body using the shared secret
configured on the webhook. Verification is mandatory and uses a constant-time comparison — a
naive ``==`` leaks the correct digest one byte at a time to an attacker who can time responses.

The body must be the exact bytes GitHub sent. Re-serialising the parsed JSON changes whitespace
and key order and would fail verification on valid deliveries, which is why the route reads
``await request.body()`` rather than using a Pydantic model.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"
LEGACY_SIGNATURE_HEADER = "X-Hub-Signature"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"

SHA256_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    """The value GitHub would put in ``X-Hub-Signature-256`` for this body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SHA256_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time verification of a webhook delivery.

    Returns ``False`` rather than raising for every failure mode, so the caller decides the
    response — and so a missing secret can never be mistaken for a valid signature.
    """
    if not secret:
        logger.warning("Rejecting a webhook delivery: no signing secret is configured.")
        return False
    if not signature:
        logger.warning("Rejecting a webhook delivery: the signature header is absent.")
        return False
    if not signature.startswith(SHA256_PREFIX):
        # SHA-1 signatures are deprecated and cryptographically broken; refuse them outright.
        logger.warning("Rejecting a webhook delivery: unsupported signature algorithm.")
        return False

    return hmac.compare_digest(compute_signature(secret, body), signature)
