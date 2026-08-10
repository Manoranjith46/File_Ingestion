"""Fernet (AES-256-CBC) credential vault for External FTP Pull sessions.

This module provides symmetric encryption using ``cryptography.fernet.Fernet``
to protect FTP passwords stored temporarily in Redis.  It is intentionally
separate from the existing AES-256-GCM helper in ``helpers/crypto.py`` so that
each encryption context remains independently keyed and auditable.

Environment variable
    ``FERNET_KEY`` — A URL-safe base64-encoded 32-byte key produced by
    ``cryptography.fernet.Fernet.generate_key()``.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from helpers.get_env import get_env

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

# Deterministic fallback for local development only.  Production deployments
# MUST set ``FERNET_KEY`` in the environment.
# Generated via Fernet.generate_key() — 32 url-safe base64-encoded bytes.
_DEV_FALLBACK_KEY = b"Op7ch2bc8vacqXPVopq2VP1RhWkUG5d7LcwmxDMDvws="


def _load_fernet_key() -> bytes:
    """Load the Fernet encryption key from the environment.

    Falls back to a deterministic development key when the variable is absent,
    logging a warning so the omission is visible during startup.

    Returns:
        bytes: A valid Fernet key (URL-safe base64, 32 bytes decoded).
    """
    raw = get_env("FERNET_KEY", default="", required=False).strip()
    if not raw:
        logger.warning(
            "FERNET_KEY is not configured; using built-in dev fallback key. "
            "Set FERNET_KEY in production!"
        )
        return _DEV_FALLBACK_KEY
    return raw.encode("ascii")


def _get_fernet() -> Fernet:
    """Return a ``Fernet`` instance initialised with the configured key.

    Returns:
        Fernet: A ready-to-use Fernet cipher object.
    """
    return Fernet(_load_fernet_key())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fernet_encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string and return the Fernet token as text.

    Args:
        plaintext (str): The secret value to encrypt (e.g. an FTP password).

    Returns:
        str: A URL-safe base64-encoded Fernet token.

    Raises:
        TypeError: If *plaintext* is not a string.
    """
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be a string")
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def fernet_decrypt(token: str) -> str:
    """Decrypt a Fernet token previously produced by :func:`fernet_encrypt`.

    Args:
        token (str): The Fernet-encrypted token string.

    Returns:
        str: The original plaintext value.

    Raises:
        TypeError: If *token* is not a string.
        cryptography.fernet.InvalidToken: If the token is invalid or tampered.
    """
    if not isinstance(token, str):
        raise TypeError("token must be a string")
    f = _get_fernet()
    return f.decrypt(token.encode("ascii")).decode("utf-8")
