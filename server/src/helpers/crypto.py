"""AES-256-GCM helpers for application-level encryption of sensitive values.

This module provides a small wrapper around `cryptography`'s AESGCM
to encrypt and decrypt short secrets such as OAuth refresh tokens.

Environment variable: `ENCRYPTION_KEY` — base64 (URL-safe) encoded 32-byte key.
"""

from __future__ import annotations

import base64
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from helpers.get_env import get_env

logger = logging.getLogger(__name__)
_FALLBACK_KEY = b"0123456789abcdef0123456789abcdef"


def _load_key() -> bytes:
    key_b64 = get_env("ENCRYPTION_KEY", default="", required=False).strip()
    if not key_b64:
        logger.warning("ENCRYPTION_KEY is not configured; using built-in fallback key")
        return _FALLBACK_KEY
    try:
        key = base64.urlsafe_b64decode(key_b64)
    except Exception as exc:
        logger.warning("ENCRYPTION_KEY is not valid base64; using built-in fallback key: %s", exc)
        return _FALLBACK_KEY
    if len(key) != 32:
        logger.warning("ENCRYPTION_KEY must decode to 32 bytes for AES-256-GCM; using built-in fallback key")
        return _FALLBACK_KEY
    return key


def encrypt_str(plaintext: str) -> str:
    """Encrypt and return a URL-safe base64 payload containing nonce+ciphertext.

    Returns a single string which can be stored in DB as text.
    """
    if plaintext is None:
        return ""
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a string")
    key = _load_key()
    aesgcm = AESGCM(key)
    # AES-GCM nonce recommended length is 12 bytes
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    payload = nonce + ct
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decrypt_str(token_b64: str) -> str:
    """Decrypt a value previously produced by `encrypt_str`.

    Legacy or test values may be stored as plain text rather than encrypted payloads,
    so this helper returns those values unchanged when they are not valid encrypted data.
    """
    if token_b64 is None:
        return ""
    if not isinstance(token_b64, str):
        raise TypeError("token_b64 must be a string")
    if not token_b64:
        return ""
    try:
        payload = base64.urlsafe_b64decode(token_b64)
    except Exception:
        return token_b64

    key = _load_key()
    if len(payload) < 13:
        return token_b64
    nonce = payload[:12]
    ct = payload[12:]
    aesgcm = AESGCM(key)
    try:
        pt = aesgcm.decrypt(nonce, ct, None)
    except Exception:
        return token_b64
    return pt.decode("utf-8")
