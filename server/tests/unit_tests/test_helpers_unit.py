from __future__ import annotations

import pytest

from helpers import crypto, jwt


@pytest.mark.parametrize(
    ("plaintext", "expected_error"),
    [
        ("secret-value", None),
        (None, None),
        (123, TypeError),
        ("", None),
    ],
)
def test_encrypt_and_decrypt_round_trip(plaintext, expected_error):
    if expected_error is not None:
        with pytest.raises(expected_error):
            crypto.encrypt_str(plaintext)
        return

    encrypted = crypto.encrypt_str(plaintext)
    assert encrypted or plaintext in {None, ""}
    assert crypto.decrypt_str(encrypted) == ("" if plaintext is None else plaintext)


@pytest.mark.parametrize(
    ("token_b64", "expected_error"),
    [
        ("plain-token", None),
        (None, None),
        (123, TypeError),
        ("", None),
    ],
)
def test_decrypt_str_handles_invalid_or_empty_input(token_b64, expected_error):
    if expected_error is not None:
        with pytest.raises(expected_error):
            crypto.decrypt_str(token_b64)
        return

    assert crypto.decrypt_str(token_b64) == ("" if token_b64 in {None, ""} else token_b64)


@pytest.mark.parametrize(
    ("password", "expected_error"),
    [
        ("correct-horse-battery-staple", None),
        ("", ValueError),
        (None, ValueError),
        (123, ValueError),
    ],
)
def test_hash_password_validates_input(password, expected_error):
    if expected_error is not None:
        with pytest.raises(expected_error):
            jwt.hash_password(password)
        return

    hashed = jwt.hash_password(password)
    assert hashed.startswith("pbkdf2_sha256$")
    assert jwt.verify_password(password, hashed) is True


@pytest.mark.parametrize(
    ("password", "password_hash", "expected"),
    [
        ("secret", None, True),
        ("wrong", None, False),
        ("", "not-a-hash", False),
        (None, None, False),
    ],
)
def test_verify_password_handles_right_wrong_and_none(password, password_hash, expected):
    if password_hash is None:
        password_hash = jwt.hash_password("secret") if password == "secret" else jwt.hash_password("other")
    assert jwt.verify_password(password, password_hash) is expected
