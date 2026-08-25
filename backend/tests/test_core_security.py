"""Credential encryption, password hashing, JWT handling and log redaction."""

from __future__ import annotations

import logging
import time

import pytest

from app.core import crypto
from app.core.logging import REDACTED, RedactingFilter, redact
from app.core.security import (
    TokenError,
    constant_time_compare,
    create_access_token,
    create_refresh_token,
    create_state_token,
    decode_token,
    hash_password,
    verify_password,
    verify_state_token,
)


# ── Credential encryption ───────────────────────────────────────────────────


def test_encrypt_decrypt_roundtrip():
    secret = "ya29.a0AfH6SMB-super-secret-refresh-token"
    ciphertext = crypto.encrypt_str(secret)
    assert ciphertext != secret
    assert secret not in ciphertext
    assert crypto.decrypt_str(ciphertext) == secret


def test_encryption_is_non_deterministic():
    """Fernet includes a random IV, so identical plaintext yields different ciphertext."""
    a = crypto.encrypt_str("same-value")
    b = crypto.encrypt_str("same-value")
    assert a != b
    assert crypto.decrypt_str(a) == crypto.decrypt_str(b) == "same-value"


def test_encrypt_decrypt_json_blob():
    blob = {"refresh_token": "1//abcdef", "access_token": "ya29.xyz", "scope": "read"}
    restored = crypto.decrypt_json(crypto.encrypt_json(blob))
    assert restored == blob


def test_decrypt_with_rotated_secret_fails_loudly(monkeypatch):
    ciphertext = crypto.encrypt_str("token")
    monkeypatch.setattr(crypto.settings, "secret_key", "a-completely-different-secret-value")
    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt_str(ciphertext)


def test_decrypt_rejects_tampered_ciphertext():
    ciphertext = crypto.encrypt_str("token")
    tampered = ciphertext[:-6] + ("A" if ciphertext[-6] != "A" else "B") + ciphertext[-5:]
    with pytest.raises(crypto.CredentialDecryptionError):
        crypto.decrypt_str(tampered)


def test_mask_secret_never_reveals_the_body():
    assert crypto.mask_secret("abcdefghijklmnop").endswith("mnop")
    assert "abcdefghijkl" not in crypto.mask_secret("abcdefghijklmnop")
    assert crypto.mask_secret(None) is None


# ── Passwords ───────────────────────────────────────────────────────────────


def test_password_hash_and_verify():
    hashed = hash_password("Correct-horse-battery9")
    assert hashed != "Correct-horse-battery9"
    assert verify_password("Correct-horse-battery9", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_password_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_password_handles_garbage_input():
    assert verify_password("", "") is False
    assert verify_password("pw", "not-a-bcrypt-hash") is False


# ── JWT ─────────────────────────────────────────────────────────────────────


def test_access_token_roundtrip():
    token = create_access_token(42, "admin", "a@example.com")
    claims = decode_token(token, expected_type="access")
    assert claims["sub"] == "42"
    assert claims["role"] == "admin"
    assert claims["email"] == "a@example.com"


def test_token_type_is_enforced():
    refresh = create_refresh_token(7)
    decode_token(refresh, expected_type="refresh")
    with pytest.raises(TokenError):
        decode_token(refresh, expected_type="access")


def test_tampered_token_is_rejected():
    token = create_access_token(1, "member", "m@example.com")
    with pytest.raises(TokenError):
        decode_token(token[:-3] + "abc")


def test_expired_token_is_rejected(monkeypatch):
    monkeypatch.setattr("app.core.security.settings.access_token_expire_minutes", -1)
    token = create_access_token(1, "member", "m@example.com")
    with pytest.raises(TokenError):
        decode_token(token, expected_type="access")


def test_tokens_are_unique_per_issue():
    a = create_access_token(1, "member", "m@example.com")
    time.sleep(0.01)
    b = create_access_token(1, "member", "m@example.com")
    assert decode_token(a)["jti"] != decode_token(b)["jti"]


# ── OAuth state ─────────────────────────────────────────────────────────────


def test_state_token_roundtrip():
    token = create_state_token(website_id=9, provider="gsc")
    claims = verify_state_token(token)
    assert claims["website_id"] == 9
    assert claims["provider"] == "gsc"


def test_access_token_is_not_accepted_as_state():
    with pytest.raises(TokenError):
        verify_state_token(create_access_token(1, "admin", "a@example.com"))


def test_constant_time_compare():
    assert constant_time_compare("abc", "abc") is True
    assert constant_time_compare("abc", "abd") is False


# ── Log redaction ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "api_key=gsk_liveKEY1234567890abcdef",
        'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlX2hlcmU',
        "GET https://api.semrush.com/?key=abcdef1234567890&type=url_organic",
        'client_secret="GOCSPX-abcdefghijklmnop"',
        "token ghp_abcdefghijklmnopqrstuvwxyz012345",
        "refresh_token: 1//0gabcdefghijklmnopqrstuv",
    ],
)
def test_redaction_removes_credentials(text):
    cleaned = redact(text)
    assert REDACTED in cleaned
    for leak in (
        "gsk_liveKEY1234567890abcdef",
        "abcdef1234567890",
        "GOCSPX-abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "1//0gabcdefghijklmnopqrstuv",
    ):
        assert leak not in cleaned


def test_redaction_leaves_ordinary_text_alone():
    message = "Crawled 1200 pages for example.com in 41s"
    assert redact(message) == message


def test_redacting_filter_scrubs_record_and_args():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling provider with api_key=gsk_secretvalue0123456789",
        args=("access_token=ya29.leakedtokenvalue",),
        exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
    assert "gsk_secretvalue0123456789" not in record.msg
    assert "ya29.leakedtokenvalue" not in record.args[0]
