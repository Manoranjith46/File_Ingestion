"""Unit tests for the Fernet credential vault (helpers/fernet_vault.py)."""

from __future__ import annotations

import os
import pytest

# Ensure a stable test key is set before the module loads
os.environ.setdefault("FERNET_KEY", "")

from helpers.fernet_vault import fernet_decrypt, fernet_encrypt


class TestFernetEncryptDecrypt:
    """Round-trip and edge-case tests for Fernet encryption."""

    def test_round_trip_basic(self):
        """Encrypt then decrypt returns the original plaintext."""
        plaintext = "my_secure_password_123!"
        token = fernet_encrypt(plaintext)
        assert fernet_decrypt(token) == plaintext

    def test_round_trip_unicode(self):
        """Encrypt then decrypt preserves Unicode characters."""
        plaintext = "pässwörd_日本語_🔑"
        token = fernet_encrypt(plaintext)
        assert fernet_decrypt(token) == plaintext

    def test_round_trip_empty_string(self):
        """Encrypt then decrypt preserves empty strings."""
        token = fernet_encrypt("")
        assert fernet_decrypt(token) == ""

    def test_different_plaintexts_produce_different_tokens(self):
        """Two different plaintexts must not produce the same token."""
        t1 = fernet_encrypt("password_one")
        t2 = fernet_encrypt("password_two")
        assert t1 != t2

    def test_same_plaintext_produces_different_tokens(self):
        """Fernet uses a random IV, so the same plaintext encrypts differently each time."""
        t1 = fernet_encrypt("same_value")
        t2 = fernet_encrypt("same_value")
        assert t1 != t2
        # Both must still decrypt to the same value
        assert fernet_decrypt(t1) == fernet_decrypt(t2)

    def test_encrypt_rejects_non_string(self):
        """fernet_encrypt must raise TypeError for non-string input."""
        with pytest.raises(TypeError, match="plaintext must be a string"):
            fernet_encrypt(12345)  # type: ignore[arg-type]

    def test_decrypt_rejects_non_string(self):
        """fernet_decrypt must raise TypeError for non-string input."""
        with pytest.raises(TypeError, match="token must be a string"):
            fernet_decrypt(12345)  # type: ignore[arg-type]

    def test_decrypt_with_invalid_token_raises(self):
        """Decrypting a tampered or garbage token raises InvalidToken."""
        from cryptography.fernet import InvalidToken

        with pytest.raises(InvalidToken):
            fernet_decrypt("not-a-valid-fernet-token")

    def test_decrypt_with_tampered_token_raises(self):
        """Modifying a valid token causes decryption to fail."""
        from cryptography.fernet import InvalidToken

        token = fernet_encrypt("secret")
        # Flip a character in the middle of the token
        tampered = token[:10] + ("A" if token[10] != "A" else "B") + token[11:]
        with pytest.raises(InvalidToken):
            fernet_decrypt(tampered)

    def test_long_plaintext(self):
        """Fernet handles long payloads correctly."""
        plaintext = "x" * 10_000
        token = fernet_encrypt(plaintext)
        assert fernet_decrypt(token) == plaintext
