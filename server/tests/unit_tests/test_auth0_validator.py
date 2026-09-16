"""Comprehensive unit and integration tests for Auth0 RS256 token verification."""

import base64
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.auth0_config import auth0_settings
from helpers.auth0_validator import verify_auth0_token, _JWKS_CACHE
from models.auth_model import Base, User
from services.auth_services import get_current_user


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(val: int) -> str:
    byte_len = (val.bit_length() + 7) // 8
    return _b64url_encode(val.to_bytes(byte_len, byteorder="big"))


@pytest.fixture(scope="module")
def rsa_keypair():
    """Generate a test RSA keypair and its JWKS representation."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    pub_numbers = public_key.public_numbers()

    kid = "test-key-id-001"
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": kid,
                "n": _int_to_b64url(pub_numbers.n),
                "e": _int_to_b64url(pub_numbers.e),
                "alg": "RS256",
            }
        ]
    }
    return private_key, public_key, kid, jwks


def _create_test_jwt(
    private_key,
    kid: str,
    payload: dict,
    alg: str = "RS256",
) -> str:
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    enc_header = _b64url_encode(json.dumps(header).encode("utf-8"))
    enc_payload = _b64url_encode(json.dumps(payload).encode("utf-8"))
    signing_input = f"{enc_header}.{enc_payload}".encode("ascii")

    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{enc_header}.{enc_payload}.{_b64url_encode(signature)}"


@pytest.fixture
def mock_jwks(monkeypatch, rsa_keypair):
    """Mock fetch_jwks to return our test RSA public key."""
    _, _, _, jwks = rsa_keypair
    _JWKS_CACHE.clear()

    import helpers.auth0_validator as validator_mod

    monkeypatch.setattr(validator_mod, "fetch_jwks", lambda *args, **kwargs: jwks)


def test_auth0_token_valid(mock_jwks, rsa_keypair):
    """Verify a valid RS256 token passes with expected claims."""
    private_key, _, kid, _ = rsa_keypair
    now = int(time.time())
    payload = {
        "sub": "auth0|654321",
        "iss": "https://test-tenant.auth0.com/",
        "aud": "https://api.fileingestion.test",
        "exp": now + 3600,
        "iat": now,
        "email": "alice@example.com",
    }
    token = _create_test_jwt(private_key, kid, payload)

    verified = verify_auth0_token(
        token,
        expected_audience="https://api.fileingestion.test",
        expected_issuer="https://test-tenant.auth0.com/",
    )
    assert verified["sub"] == "auth0|654321"
    assert verified["email"] == "alice@example.com"


def test_auth0_token_expired(mock_jwks, rsa_keypair):
    """Verify an expired token is rejected with 401."""
    private_key, _, kid, _ = rsa_keypair
    now = int(time.time())
    payload = {
        "sub": "auth0|expired",
        "iss": "https://test-tenant.auth0.com/",
        "aud": "https://api.fileingestion.test",
        "exp": now - 60,
        "iat": now - 3600,
    }
    token = _create_test_jwt(private_key, kid, payload)

    with pytest.raises(HTTPException) as exc:
        verify_auth0_token(
            token,
            expected_audience="https://api.fileingestion.test",
            expected_issuer="https://test-tenant.auth0.com/",
        )
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


def test_auth0_token_wrong_audience(mock_jwks, rsa_keypair):
    """Verify token with wrong audience is rejected."""
    private_key, _, kid, _ = rsa_keypair
    now = int(time.time())
    payload = {
        "sub": "auth0|wrong_aud",
        "iss": "https://test-tenant.auth0.com/",
        "aud": "https://other.audience.com",
        "exp": now + 3600,
    }
    token = _create_test_jwt(private_key, kid, payload)

    with pytest.raises(HTTPException) as exc:
        verify_auth0_token(
            token,
            expected_audience="https://api.fileingestion.test",
            expected_issuer="https://test-tenant.auth0.com/",
        )
    assert exc.value.status_code == 401
    assert "audience" in exc.value.detail.lower()


def test_auth0_token_wrong_issuer(mock_jwks, rsa_keypair):
    """Verify token with wrong issuer is rejected."""
    private_key, _, kid, _ = rsa_keypair
    now = int(time.time())
    payload = {
        "sub": "auth0|wrong_iss",
        "iss": "https://rogue-tenant.auth0.com/",
        "aud": "https://api.fileingestion.test",
        "exp": now + 3600,
    }
    token = _create_test_jwt(private_key, kid, payload)

    with pytest.raises(HTTPException) as exc:
        verify_auth0_token(
            token,
            expected_audience="https://api.fileingestion.test",
            expected_issuer="https://test-tenant.auth0.com/",
        )
    assert exc.value.status_code == 401
    assert "issuer" in exc.value.detail.lower()


def test_auth0_token_invalid_signature(mock_jwks, rsa_keypair):
    """Verify token signed with an untrusted private key fails signature verification."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _, _, kid, _ = rsa_keypair
    now = int(time.time())
    payload = {
        "sub": "auth0|attacker",
        "iss": "https://test-tenant.auth0.com/",
        "aud": "https://api.fileingestion.test",
        "exp": now + 3600,
    }
    token = _create_test_jwt(other_key, kid, payload)

    with pytest.raises(HTTPException) as exc:
        verify_auth0_token(
            token,
            expected_audience="https://api.fileingestion.test",
            expected_issuer="https://test-tenant.auth0.com/",
        )
    assert exc.value.status_code == 401
    assert "signature" in exc.value.detail.lower()

    # Clear cache so subsequent tests re-populate cleanly
    _JWKS_CACHE.clear()


def test_auth0_token_malformed():
    """Verify malformed tokens return 401."""
    with pytest.raises(HTTPException) as exc:
        verify_auth0_token("not.a.valid.jwt.token")
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc2:
        verify_auth0_token("header.payload")
    assert exc2.value.status_code == 401


def test_get_current_user_with_auth0_token(monkeypatch, mock_jwks, rsa_keypair):
    """Verify get_current_user resolves or provisions a local user from a valid Auth0 token."""
    private_key, _, kid, _ = rsa_keypair
    now = int(time.time())

    # Set auth0_settings
    monkeypatch.setattr(auth0_settings, "audience", "https://api.fileingestion.test")
    monkeypatch.setattr(auth0_settings, "issuer", "https://test-tenant.auth0.com/")

    # Setup in-memory DB
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()

    payload = {
        "sub": "auth0|user_999",
        "iss": "https://test-tenant.auth0.com/",
        "aud": "https://api.fileingestion.test",
        "exp": now + 3600,
        "email": "auth0_user@example.com",
        "name": "Auth0 User",
    }
    token = _create_test_jwt(private_key, kid, payload)

    user = get_current_user(session, token)
    assert user is not None
    assert user.auth0_sub == "auth0|user_999"
    assert user.email == "auth0_user@example.com"
    assert user.is_active is True

    # Calling again should retrieve the same user without re-creating
    user2 = get_current_user(session, token)
    assert user2.id == user.id

    session.close()


def test_get_current_user_inactive_user_rejected(monkeypatch, mock_jwks, rsa_keypair):
    """Verify an inactive user is rejected with 401."""
    private_key, _, kid, _ = rsa_keypair
    now = int(time.time())

    monkeypatch.setattr(auth0_settings, "audience", "https://api.fileingestion.test")
    monkeypatch.setattr(auth0_settings, "issuer", "https://test-tenant.auth0.com/")

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()

    user = User(
        email="inactive@example.com",
        username="inactive",
        password_hash="dummy",
        auth0_sub="auth0|inactive_user",
        is_active=False,
    )
    session.add(user)
    session.commit()

    payload = {
        "sub": "auth0|inactive_user",
        "iss": "https://test-tenant.auth0.com/",
        "aud": "https://api.fileingestion.test",
        "exp": now + 3600,
        "email": "inactive@example.com",
    }
    token = _create_test_jwt(private_key, kid, payload)

    with pytest.raises(HTTPException) as exc:
        get_current_user(session, token)
    assert exc.value.status_code == 401
    assert "inactive" in exc.value.detail.lower()

    session.close()
