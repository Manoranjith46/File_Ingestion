"""
Module 1 — Integration Tests: Authentication & Security Gateway
===============================================================
Tests all HTTP endpoints in /auth/* and /v1/auth/* using FastAPI's
synchronous TestClient (httpx transport) with:
  - In-memory SQLite substituted via FastAPI dependency_overrides
  - FakeRedis patched on auth_services module (monkeypatched at session scope)
  - No real PostgreSQL or Redis required

OTP note: generate_otp_code() is hardcoded to "123456" in dev mode.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Set required env vars BEFORE importing any application module
os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret-integration")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret-integration")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("OTP_EXPIRE_MINUTES", "10")
os.environ.setdefault("PASSWORD_RESET_EXPIRE_MINUTES", "15")
os.environ.setdefault("MAX_ACTIVE_SESSIONS", "3")

from config.database import get_db  # noqa: E402
from models.auth_model import Base  # noqa: E402
from services import auth_services  # noqa: E402

# Import app AFTER env vars are set
from main import app  # noqa: E402

DEV_OTP = "123456"


# ===========================================================================
# FakeRedis + fake limiter (same pattern as unit tests)
# ===========================================================================

class FakeRedis:
    """Minimal in-memory Redis stub for integration tests."""

    def __init__(self) -> None:
        self._zsets: dict[str, dict[str, float]] = {}

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._zsets.setdefault(key, {}).update(mapping)

    def zscore(self, key: str, member: str) -> float | None:
        return self._zsets.get(key, {}).get(member)

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, {}))

    def zrem(self, key: str, *members: str) -> int:
        zset = self._zsets.get(key, {})
        removed = 0
        for m in members:
            if m in zset:
                del zset[m]
                removed += 1
        return removed

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        zset = self._zsets.get(key, {})
        to_remove = [m for m, s in zset.items() if min_score <= s <= max_score]
        for m in to_remove:
            del zset[m]
        return len(to_remove)

    def zremrangebyrank(self, key: str, start: int, stop: int) -> int:
        zset = self._zsets.get(key, {})
        sorted_members = sorted(zset.items(), key=lambda x: x[1])
        if stop < 0:
            stop = len(sorted_members) + stop
        to_remove = [m for m, _ in sorted_members[start : stop + 1]]
        for m in to_remove:
            del zset[m]
        return len(to_remove)

    def delete(self, *keys: str) -> None:
        for key in keys:
            self._zsets.pop(key, None)

    def expire(self, key: str, seconds: int) -> None:
        pass


def make_fake_limiter(fake_redis: FakeRedis):
    def _limiter(keys, args):
        key = keys[0]
        now = int(args[0])
        sid = args[1]
        max_sessions = int(args[2])
        ttl = int(args[3])
        fake_redis.zremrangebyscore(key, float("-inf"), now - ttl)
        fake_redis.zadd(key, {sid: float(now)})
        card = fake_redis.zcard(key)
        if card > max_sessions:
            fake_redis.zremrangebyrank(key, 0, card - max_sessions - 1)
        fake_redis.expire(key, ttl)
        return 1
    return _limiter


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope="module")
def _engine():
    """Create a module-scoped in-memory SQLite engine."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def _session_factory(_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db_session(_session_factory) -> Generator[Session, None, None]:
    """Provide a fresh, auto-rolled-back DB session per test."""
    connection = _session_factory().bind.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def fake_redis_instance() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def client(db_session: Session, fake_redis_instance: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """
    TestClient with:
      - get_db overridden to use in-memory SQLite session
      - auth_services Redis patched to FakeRedis
    """
    monkeypatch.setattr(auth_services, "redis_server", fake_redis_instance)
    monkeypatch.setattr(auth_services, "active_session_limiter", make_fake_limiter(fake_redis_instance))

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    app.dependency_overrides.clear()


# ===========================================================================
# Helpers
# ===========================================================================

def _register_and_verify(client: TestClient, email: str, username: str, password: str = "password123") -> dict:
    """Register + OTP-verify a user, return the verify response JSON."""
    reg = client.post("/auth/signup/init", json={"email": email, "username": username, "password": password})
    assert reg.status_code == 200, f"Registration failed: {reg.text}"
    verify = client.post("/auth/signup/verify", json={"email": email, "otp_code": DEV_OTP})
    assert verify.status_code == 200, f"Verify failed: {verify.text}"
    return verify.json()


def _login(client: TestClient, identifier: str, password: str = "password123") -> tuple[str, str]:
    """Login and return (access_token, refresh_token_cookie)."""
    resp = client.post("/auth/login", json={"identifier": identifier, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    access_token = resp.headers.get("authorization", "").removeprefix("Bearer ").strip()
    refresh_cookie = resp.cookies.get("refresh_token", "")
    return access_token, refresh_cookie


# ===========================================================================
# I-A-01 — POST /auth/signup/init — Happy path
# ===========================================================================
def test_signup_init_happy_path(client: TestClient) -> None:
    """Registration should return 200 with RegistrationResponse shape."""
    payload = {"email": "init_ok@example.com", "username": "init_ok", "password": "password123"}
    resp = client.post("/auth/signup/init", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert "user" in body
    assert body["user"]["email"] == "init_ok@example.com"
    assert body["user"]["is_verified"] is False
    assert "message" in body


# ===========================================================================
# I-A-02 — POST /auth/signup/init — Missing required fields → 422
# ===========================================================================
def test_signup_init_missing_fields_returns_422(client: TestClient) -> None:
    """Payload without 'password' must return 422 Unprocessable Entity."""
    resp = client.post("/auth/signup/init", json={"email": "nopw@example.com"})
    assert resp.status_code == 422


# ===========================================================================
# I-A-03 — POST /auth/signup/init — Duplicate email → 409
# ===========================================================================
def test_signup_init_duplicate_email_returns_409(client: TestClient) -> None:
    """Registering the same email twice must return 409 Conflict."""
    payload = {"email": "dup_email@example.com", "username": "dup_email_user", "password": "password123"}
    first = client.post("/auth/signup/init", json=payload)
    assert first.status_code == 200

    second = client.post("/auth/signup/init", json=payload)
    assert second.status_code == 409


# ===========================================================================
# I-A-04 — POST /auth/signup/verify — Valid OTP → 200 + cookie + header
# ===========================================================================
def test_signup_verify_happy_path(client: TestClient) -> None:
    """Valid OTP must return 200, set refresh_token cookie, and Authorization header."""
    email = "verify_ok@example.com"
    client.post("/auth/signup/init", json={"email": email, "username": "verify_ok", "password": "password123"})

    resp = client.post("/auth/signup/verify", json={"email": email, "otp_code": DEV_OTP})

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["user"]["is_verified"] is True
    # Authorization header must be present
    assert "authorization" in resp.headers
    # HttpOnly cookie must be set
    assert "refresh_token" in resp.cookies
    assert "access_token" in resp.cookies


# ===========================================================================
# I-A-05 — POST /auth/signup/verify — Wrong OTP → 400
# ===========================================================================
def test_signup_verify_wrong_otp_returns_400(client: TestClient) -> None:
    """Submitting a wrong OTP must return 400 Bad Request."""
    email = "wrong_otp@example.com"
    client.post("/auth/signup/init", json={"email": email, "username": "wrong_otp", "password": "password123"})

    resp = client.post("/auth/signup/verify", json={"email": email, "otp_code": "000000"})
    assert resp.status_code == 400


# ===========================================================================
# I-A-06 — POST /auth/login — Valid verified user → 200 + token pair
# ===========================================================================
def test_login_happy_path(client: TestClient) -> None:
    """Login with correct credentials must return 200 with access_token and cookie."""
    _register_and_verify(client, "login_ok@example.com", "login_ok")

    resp = client.post("/auth/login", json={"identifier": "login_ok@example.com", "password": "password123"})

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "authorization" in resp.headers
    assert "refresh_token" in resp.cookies
    assert "access_token" in resp.cookies


# ===========================================================================
# I-A-07 — POST /auth/login — Unverified user → 403
# ===========================================================================
def test_login_unverified_user_returns_403(client: TestClient) -> None:
    """Login without completing OTP verification must return 403 Forbidden."""
    # Register but do NOT verify
    client.post("/auth/signup/init", json={
        "email": "unverified_login@example.com",
        "username": "unverified_login",
        "password": "password123",
    })

    resp = client.post("/auth/login", json={"identifier": "unverified_login@example.com", "password": "password123"})
    assert resp.status_code == 403


# ===========================================================================
# I-A-08 — POST /auth/login — Wrong credentials → 401
# ===========================================================================
def test_login_wrong_credentials_returns_401(client: TestClient) -> None:
    """Wrong password must return 401 Unauthorized."""
    _register_and_verify(client, "cred_fail@example.com", "cred_fail")

    resp = client.post("/auth/login", json={"identifier": "cred_fail@example.com", "password": "wrongpassword"})
    assert resp.status_code == 401


# ===========================================================================
# I-A-09 — POST /auth/login — Missing password field → 422
# ===========================================================================
def test_login_missing_password_returns_422(client: TestClient) -> None:
    """Payload without 'password' must return 422 Unprocessable Entity."""
    resp = client.post("/auth/login", json={"identifier": "someone@example.com"})
    assert resp.status_code == 422


# ===========================================================================
# I-A-10 — GET /auth/me — Valid Bearer token → 200 + PublicUserSchema
# ===========================================================================
def test_me_happy_path(client: TestClient) -> None:
    """Valid access token must return 200 with full public user profile."""
    _register_and_verify(client, "me_ok@example.com", "me_ok")
    access_tok, _ = _login(client, "me_ok@example.com")

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {access_tok}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "me_ok@example.com"
    assert "password_hash" not in body
    assert "id" in body


# ===========================================================================
# I-A-11 — GET /auth/me — Missing Authorization header → 401
# ===========================================================================
def test_me_missing_auth_returns_401(client: TestClient) -> None:
    """Request without Authorization header must return 401 Unauthorized."""
    resp = client.get("/auth/me")
    assert resp.status_code == 401


# ===========================================================================
# I-A-12 — GET /auth/me — Malformed Bearer token → 401
# ===========================================================================
def test_me_malformed_token_returns_401(client: TestClient) -> None:
    """A garbled access token must return 401 Unauthorized."""
    resp = client.get("/auth/me", headers={"Authorization": "Bearer this.is.garbage"})
    assert resp.status_code == 401


# ===========================================================================
# I-A-13 — POST /auth/refresh — Valid refresh_token cookie → rotated pair
# ===========================================================================
def test_refresh_happy_path(client: TestClient) -> None:
    """A valid refresh_token cookie must yield a fresh access token and new cookie."""
    _register_and_verify(client, "refresh_ok@example.com", "refresh_ok")
    access_tok, refresh_cookie = _login(client, "refresh_ok@example.com")

    # Set cookie manually for TestClient
    client.cookies.set("refresh_token", refresh_cookie, path="/auth")
    resp = client.post("/auth/refresh")

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    new_access = resp.headers.get("authorization", "").removeprefix("Bearer ").strip()
    assert new_access  # new token issued
    # New refresh cookie must be set
    assert "refresh_token" in resp.cookies
    assert "access_token" in resp.cookies


# ===========================================================================
# I-A-14 — POST /auth/refresh — Missing cookie → 401
# ===========================================================================
def test_refresh_missing_cookie_returns_401(client: TestClient) -> None:
    """Request with no refresh_token cookie must return 401 Unauthorized."""
    # Clear any cookies that might have been set
    client.cookies.clear()
    resp = client.post("/auth/refresh")
    assert resp.status_code == 401


# ===========================================================================
# I-A-15 — POST /auth/logout — Valid access token → 200 + cookie cleared
# ===========================================================================
def test_logout_happy_path(client: TestClient) -> None:
    """Logout with a valid Bearer token must return 200 and clear the cookie."""
    _register_and_verify(client, "logout_ok@example.com", "logout_ok")
    access_tok, refresh_cookie = _login(client, "logout_ok@example.com")

    client.cookies.set("refresh_token", refresh_cookie, path="/auth")
    resp = client.post("/auth/logout", headers={"Authorization": f"Bearer {access_tok}"})

    assert resp.status_code == 200
    assert resp.json()["message"] == "Logged out successfully"
    assert "access_token" not in resp.cookies


# ===========================================================================
# I-A-16 — POST /auth/logout — Missing Authorization → 401
# ===========================================================================
def test_logout_missing_auth_returns_401(client: TestClient) -> None:
    """Logout without Authorization header must return 401 Unauthorized."""
    resp = client.post("/auth/logout")
    assert resp.status_code == 401


# ===========================================================================
# I-A-17 — POST /auth/password-reset/request — Known email → 200 + reset_token
# ===========================================================================
def test_password_reset_request_happy_path(client: TestClient) -> None:
    """Password reset request for a registered user must return reset_token."""
    _register_and_verify(client, "prr_ok@example.com", "prr_ok")

    resp = client.post("/auth/password-reset/request", json={"email": "prr_ok@example.com"})

    assert resp.status_code == 200
    body = resp.json()
    assert "reset_token" in body
    assert body["email"] == "prr_ok@example.com"


# ===========================================================================
# I-A-18 — POST /auth/password-reset/request — Unknown email → 404
# ===========================================================================
def test_password_reset_request_unknown_email_returns_404(client: TestClient) -> None:
    """Password reset for an unknown email must return 404 Not Found."""
    resp = client.post("/auth/password-reset/request", json={"email": "nobody404@example.com"})
    assert resp.status_code == 404


# ===========================================================================
# I-A-19 — POST /auth/password-reset/verify — Valid token → 200 success message
# ===========================================================================
def test_password_reset_verify_happy_path(client: TestClient) -> None:
    """Using a valid reset token must return 200 with success message."""
    _register_and_verify(client, "prv_ok@example.com", "prv_ok")
    req_resp = client.post("/auth/password-reset/request", json={"email": "prv_ok@example.com"})
    reset_token = req_resp.json()["reset_token"]

    resp = client.post(
        "/auth/password-reset/verify",
        json={"email": "prv_ok@example.com", "reset_token": reset_token, "new_password": "newpassword1"},
    )

    assert resp.status_code == 200
    assert "password updated" in resp.json()["message"].lower()


# ===========================================================================
# I-A-20 — POST /auth/password-reset/verify — Invalid reset token → 400
# ===========================================================================
def test_password_reset_verify_invalid_token_returns_400(client: TestClient) -> None:
    """Supplying a wrong reset token must return 400 Bad Request."""
    _register_and_verify(client, "prv_bad@example.com", "prv_bad")
    client.post("/auth/password-reset/request", json={"email": "prv_bad@example.com"})

    resp = client.post(
        "/auth/password-reset/verify",
        json={"email": "prv_bad@example.com", "reset_token": "invalid-token-value-xyz", "new_password": "newpassword1"},
    )
    assert resp.status_code == 400


# ===========================================================================
# I-A-21 — /v1/auth/* alias — Dual-mount endpoints respond identically
# ===========================================================================
def test_v1_auth_alias_signup_init(client: TestClient) -> None:
    """The /v1/auth/signup/init alias must behave identically to /auth/signup/init."""
    payload = {"email": "v1alias@example.com", "username": "v1alias", "password": "password123"}
    resp = client.post("/v1/auth/signup/init", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == "v1alias@example.com"


def test_v1_auth_alias_login(client: TestClient) -> None:
    """The /v1/auth/login alias must work after registration on /v1/auth/."""
    email = "v1login@example.com"
    username = "v1login"

    # Register + verify via v1 alias
    client.post("/v1/auth/signup/init", json={"email": email, "username": username, "password": "password123"})
    client.post("/v1/auth/signup/verify", json={"email": email, "otp_code": DEV_OTP})

    resp = client.post("/v1/auth/login", json={"identifier": email, "password": "password123"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()
