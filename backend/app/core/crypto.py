"""Encryption for third-party credentials at rest.

Integration credentials (OAuth refresh tokens, API keys, webhook secrets) are stored as Fernet
ciphertext. The key is derived from ``SECRET_KEY`` with HKDF so that operators only manage a single
secret, while the encryption key is domain-separated from the JWT signing key.

Ciphertext never leaves the process boundary: API responses expose integration *status* and
non-sensitive metadata only.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..config import settings

_INFO = b"seo-automation:credential-encryption:v1"


class CredentialDecryptionError(RuntimeError):
    """Raised when stored ciphertext cannot be decrypted with the current key."""


def _derive_key(secret: str) -> bytes:
    """Derive a URL-safe base64 32-byte Fernet key from the application secret."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_INFO,
    )
    return base64.urlsafe_b64encode(hkdf.derive(secret.encode("utf-8")))


_fernet: Fernet | None = None
_fernet_secret: str | None = None


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance, rebuilding it if the secret changed (tests)."""
    global _fernet, _fernet_secret
    if _fernet is None or _fernet_secret != settings.secret_key:
        _fernet = Fernet(_derive_key(settings.secret_key))
        _fernet_secret = settings.secret_key
    return _fernet


def encrypt_str(plaintext: str) -> str:
    """Encrypt a UTF-8 string, returning ASCII ciphertext safe for a text column."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_str(ciphertext: str) -> str:
    """Decrypt ciphertext produced by :func:`encrypt_str`."""
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise CredentialDecryptionError(
            "Stored credential could not be decrypted; SECRET_KEY may have changed."
        ) from exc


def encrypt_json(payload: dict[str, Any]) -> str:
    """Encrypt a JSON-serialisable mapping (the shape used for credential blobs)."""
    return encrypt_str(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def decrypt_json(ciphertext: str) -> dict[str, Any]:
    """Decrypt a blob written by :func:`encrypt_json`."""
    data = json.loads(decrypt_str(ciphertext))
    if not isinstance(data, dict):  # pragma: no cover - defensive
        raise CredentialDecryptionError("Decrypted credential blob is not an object.")
    return data


def mask_secret(value: str | None, keep: int = 4) -> str | None:
    """Return a display-safe representation of a secret, e.g. ``"****abcd"``.

    Used for UI affordances such as "which API key is configured?" without ever returning the
    secret itself.
    """
    if not value:
        return None
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * max(4, len(value) - keep) + value[-keep:]
