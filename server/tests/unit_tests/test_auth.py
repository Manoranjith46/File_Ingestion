"""
Module 1 — Unit Tests: Authentication & Security Gateway
=========================================================
Covers: create_user, authenticate_user, issue_token_pair, resolve_refresh_user,
        get_current_user, verify_otp, request_otp, request_password_reset,
        reset_password, revoke_session, JWT helpers, password hashing.

Isolation strategy:
  - In-memory SQLite via SQLAlchemy (rolled back / dropped after each test).
  - FakeRedis stub replaces real Redis (monkeypatched on auth_services module).
  - No network I/O, no real PostgreSQL required.

OTP note: generate_otp_code() is currently hardcoded to return "123456" (dev mode).
All OTP tests use "123456" and are marked so they survive a future fix.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Generator

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Path bootstrap — ensure src/ is importable without the "src." prefix
# ---------------------------------------------------------------------------
SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Set required env vars BEFORE importing any application module
os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("OTP_EXPIRE_MINUTES", "10")
os.environ.setdefault("PASSWORD_RESET_EXPIRE_MINUTES", "15")
os.environ.setdefault("MAX_ACTIVE_SESSIONS", "3")

from models.auth_model import Base, User  # noqa: E402
from schemas.auth_schema import (  # noqa: E402
    LoginRequest,
    OtpVerifyRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RegisterRequest,
)
from routes import auth_routes  # noqa: E402
from services import auth_services  # noqa: E402
from helpers import jwt as jwt_helpers  # noqa: E402


# ===========================================================================
# Shared helpers
# ===========================================================================

DEV_OTP = "123456"  # generate_otp_code() is hardcoded to this in dev mode


# ===========================================================================
# Fixtures
# ===========================================================================


class FakeRedis:
    """In-memory Redis stub — covers zscore, zrem, zcard, zadd, delete, expire."""

    def __init__(self) -> None:
        self._zsets: dict[str, dict[str, float]] = {}

    # --- ZSET helpers -------------------------------------------------------
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
        # Handle negative indices
        if stop < 0:
            stop = len(sorted_members) + stop
        to_remove = [m for m, _ in sorted_members[start : stop + 1]]
        for m in to_remove:
            del zset[m]
        return len(to_remove)

    # --- General helpers -----------------------------------------------------
    def delete(self, *keys: str) -> None:
        for key in keys:
            self._zsets.pop(key, None)

    def expire(self, key: str, seconds: int) -> None:
        pass  # no-op in stub


def make_fake_limiter(fake_redis: FakeRedis):
    """Return a callable that mimics the active_session_limiter Lua script."""

    def _limiter(keys, args):
        key = keys[0]
        now = int(args[0])
        sid = args[1]
        max_sessions = int(args[2])
        ttl = int(args[3])

        # Remove expired (score < now - ttl)
        fake_redis.zremrangebyscore(key, float("-inf"), now - ttl)
        # Add new session
        fake_redis.zadd(key, {sid: float(now)})
        # Enforce max
        card = fake_redis.zcard(key)
        if card > max_sessions:
            fake_redis.zremrangebyrank(key, 0, card - max_sessions - 1)
        fake_redis.expire(key, ttl)
        return 1

    return _limiter


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Isolated in-memory SQLite session — created fresh per test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Patch auth_services to use in-memory Redis stub + realistic Lua limiter."""
    stub = FakeRedis()
    monkeypatch.setattr(auth_services, "redis_server", stub)
    monkeypatch.setattr(auth_services, "active_session_limiter", make_fake_limiter(stub))
    return stub


@pytest.fixture()
def verified_user(db_session: Session, fake_redis: FakeRedis) -> User:
    """Create and return a verified user in the test DB."""
    payload = RegisterRequest(
        email="verified@example.com",
        username="verified_user",
        full_name="Verified User",
        password="strongpass1",
    )
    user = auth_services.create_user(db_session, payload)
    user.is_verified = True
    db_session.commit()
    db_session.refresh(user)
    return user


# ===========================================================================
# U-A-01 — create_user: happy path
# ===========================================================================
def test_refresh_accepts_refresh_token_from_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refresh endpoint should accept a refresh token passed via header for browser-safe flows."""

    monkeypatch.setattr(auth_routes, "resolve_refresh_user", lambda db, token: object())
    monkeypatch.setattr(auth_routes, "issue_token_pair", lambda db, user: (object(), "new-access-token", "new-refresh-token"))

    response = Response()
    auth_routes.refresh(response, refresh_token=None, x_refresh_token="header-refresh-token", db=None)

    assert response.headers["Authorization"] == "Bearer new-access-token"
    assert response.headers["X-Refresh-Token"] == "new-refresh-token"


def test_login_sets_refresh_cookie_on_root_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login should issue a refresh cookie that is visible to API subpaths in the browser."""

    monkeypatch.setattr(auth_routes, "authenticate_user", lambda db, payload: object())
    monkeypatch.setattr(auth_routes, "issue_token_pair", lambda db, user: (object(), "access-token", "refresh-token"))

    response = Response()
    auth_routes.login(LoginRequest(identifier="user@example.com", password="password123"), response, db=None)

    assert "refresh_token=refresh-token" in response.headers["set-cookie"]
    assert "Path=/" in response.headers["set-cookie"]


def test_create_user_happy_path(db_session: Session) -> None:
    """New user should be persisted with correct fields and is_verified=False."""
    payload = RegisterRequest(
        email="new@example.com",
        username="newuser",
        full_name="New User",
        password="password123",
    )
    user = auth_services.create_user(db_session, payload)

    assert user.id is not None
    assert user.email == "new@example.com"
    assert user.username == "newuser"
    assert user.full_name == "New User"
    assert user.auth_provider == "local"
    assert user.is_verified is False
    assert user.password_hash != "password123"  # must be hashed


# ===========================================================================
# U-A-02 — create_user: duplicate email → 409
# ===========================================================================
def test_create_user_duplicate_email_raises_409(db_session: Session) -> None:
    """Registering with an already-used email must raise HTTP 409 Conflict."""
    payload = RegisterRequest(email="dup@example.com", password="password123")
    auth_services.create_user(db_session, payload)

    with pytest.raises(HTTPException) as exc:
        auth_services.create_user(db_session, payload)

    assert exc.value.status_code == 409
    assert "email" in exc.value.detail.lower()


# ===========================================================================
# U-A-03 — create_user: duplicate username → 409
# ===========================================================================
def test_create_user_duplicate_username_raises_409(db_session: Session) -> None:
    """Registering with an already-used username must raise HTTP 409 Conflict."""
    auth_services.create_user(
        db_session, RegisterRequest(email="a@example.com", username="dupname", password="password123")
    )
    with pytest.raises(HTTPException) as exc:
        auth_services.create_user(
            db_session, RegisterRequest(email="b@example.com", username="dupname", password="password123")
        )

    assert exc.value.status_code == 409
    assert "username" in exc.value.detail.lower()


# ===========================================================================
# U-A-04 — authenticate_user: valid verified user
# ===========================================================================
def test_authenticate_user_happy_path(db_session: Session) -> None:
    """Correct credentials for a verified user should return the user object."""
    payload = RegisterRequest(email="auth@example.com", username="authuser", password="mypassword1")
    user = auth_services.create_user(db_session, payload)
    user.is_verified = True
    db_session.commit()
    db_session.refresh(user)

    result = auth_services.authenticate_user(db_session, LoginRequest(identifier="auth@example.com", password="mypassword1"))
    assert result.email == "auth@example.com"
    assert result.last_login_at is not None


# ===========================================================================
# U-A-05 — authenticate_user: wrong password → 401
# ===========================================================================
def test_authenticate_user_wrong_password_raises_401(db_session: Session) -> None:
    """Wrong password must raise HTTP 401 Unauthorized."""
    payload = RegisterRequest(email="wrong@example.com", username="wrongpass", password="correctpass1")
    user = auth_services.create_user(db_session, payload)
    user.is_verified = True
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        auth_services.authenticate_user(db_session, LoginRequest(identifier="wrong@example.com", password="badpassword"))
    assert exc.value.status_code == 401


# ===========================================================================
# U-A-06 — authenticate_user: unverified user → 403
# ===========================================================================
def test_authenticate_user_unverified_raises_403(db_session: Session) -> None:
    """Unverified accounts must be blocked with HTTP 403 Forbidden."""
    payload = RegisterRequest(email="unverified@example.com", username="unverifuser", password="password123")
    auth_services.create_user(db_session, payload)  # is_verified=False by default

    with pytest.raises(HTTPException) as exc:
        auth_services.authenticate_user(db_session, LoginRequest(identifier="unverified@example.com", password="password123"))
    assert exc.value.status_code == 403


# ===========================================================================
# U-A-07 — authenticate_user: unknown user → 401
# ===========================================================================
def test_authenticate_user_unknown_user_raises_401(db_session: Session) -> None:
    """Authentication for a non-existent user must raise HTTP 401 Unauthorized."""
    with pytest.raises(HTTPException) as exc:
        auth_services.authenticate_user(db_session, LoginRequest(identifier="ghost@example.com", password="password123"))
    assert exc.value.status_code == 401


# ===========================================================================
# U-A-08 — issue_token_pair: returns signed access + refresh + session
# ===========================================================================
def test_issue_token_pair_handles_none_token_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """issue_token_pair should initialize token_version when the user has no value yet."""

    class DummySession:
        def commit(self) -> None:
            return None

        def refresh(self, instance: object) -> None:
            return None

    monkeypatch.setattr(auth_services, "active_session_limiter", lambda **_: None)

    user = User(
        id="tp-user-none",
        email="tp-none@example.com",
        username="tpnone",
        password_hash="hash",
        role="user",
        auth_provider="local",
        is_verified=True,
        token_version=None,
    )

    token_resp, access_tok, refresh_tok = auth_services.issue_token_pair(DummySession(), user)

    assert token_resp.user.email == "tp-none@example.com"
    assert access_tok.startswith("ey")
    assert refresh_tok.startswith("ey")
    assert user.token_version == 1


def test_issue_token_pair_returns_valid_tokens(db_session: Session, fake_redis: FakeRedis) -> None:
    """issue_token_pair must return a TokenPairResponse + two signed JWT strings."""
    user = User(
        id="tp-user-1",
        email="tp@example.com",
        username="tpuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    token_resp, access_tok, refresh_tok = auth_services.issue_token_pair(db_session, user)

    assert token_resp.user.email == "tp@example.com"
    # Custom JWTs start with "ey" (base64 encoded header)
    assert access_tok.startswith("ey")
    assert refresh_tok.startswith("ey")
    assert token_resp.access_token == access_tok


# ===========================================================================
# U-A-09 — issue_token_pair: Redis session cap (max_sessions=2)
# ===========================================================================
def test_issue_token_pair_enforces_session_cap(db_session: Session, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """When max_sessions=2, the 3rd login should evict the oldest session."""
    monkeypatch.setenv("MAX_ACTIVE_SESSIONS", "2")

    user = User(
        id="cap-user-1",
        email="cap@example.com",
        username="capuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Issue 3 token pairs — the limiter should keep only 2
    auth_services.issue_token_pair(db_session, user)
    db_session.refresh(user)
    auth_services.issue_token_pair(db_session, user)
    db_session.refresh(user)
    auth_services.issue_token_pair(db_session, user)
    db_session.refresh(user)

    zset_key = f"user:sessions:{user.id}"
    assert fake_redis.zcard(zset_key) <= 2


# ===========================================================================
# U-A-10 — resolve_refresh_user: valid token with Redis session present
# ===========================================================================
def test_resolve_refresh_user_happy_path(db_session: Session, fake_redis: FakeRedis) -> None:
    """Valid refresh token whose sid exists in Redis should resolve to the user."""
    user = User(
        id="rr-user-1",
        email="rr@example.com",
        username="rruser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Issue a real token pair (populates fake_redis)
    _, _, refresh_tok = auth_services.issue_token_pair(db_session, user)
    db_session.refresh(user)

    resolved = auth_services.resolve_refresh_user(db_session, refresh_tok)
    assert resolved.id == user.id


# ===========================================================================
# U-A-11 — resolve_refresh_user: evicted session (sid not in Redis) → 401
# ===========================================================================
def test_resolve_refresh_user_evicted_session_raises_401(db_session: Session, fake_redis: FakeRedis) -> None:
    """If the sid is not in Redis (evicted), resolve must raise HTTP 401."""
    user = User(
        id="evict-user-1",
        email="evict@example.com",
        username="evictuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Build a refresh token with a known sid, but DON'T add it to fake_redis
    refresh_tok = jwt_helpers.create_refresh_token(user, sid="orphan-sid-999")

    with pytest.raises(HTTPException) as exc:
        auth_services.resolve_refresh_user(db_session, refresh_tok)
    assert exc.value.status_code == 401
    assert "evicted" in exc.value.detail.lower() or "session" in exc.value.detail.lower()


# ===========================================================================
# U-A-12 — resolve_refresh_user: mismatched token_version → 401
# ===========================================================================
def test_resolve_refresh_user_stale_token_version_raises_401(db_session: Session, fake_redis: FakeRedis) -> None:
    """A refresh token with an outdated token_version must raise HTTP 401."""
    user = User(
        id="tv-user-1",
        email="tv@example.com",
        username="tvuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Create token when token_version=0
    refresh_tok = jwt_helpers.create_refresh_token(user, sid="sid-tv-1")
    # Now populate Redis so sid exists but token_version is wrong
    zset_key = f"user:sessions:{user.id}"
    fake_redis.zadd(zset_key, {"sid-tv-1": 9999999999.0})

    # Bump token_version in DB (simulates logout/revocation)
    user.token_version = 5
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        auth_services.resolve_refresh_user(db_session, refresh_tok)
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower()


# ===========================================================================
# U-A-13 — get_current_user: valid access token
# ===========================================================================
def test_get_current_user_happy_path(db_session: Session, fake_redis: FakeRedis) -> None:
    """A valid, unexpired access token should resolve to the correct user."""
    user = User(
        id="cu-user-1",
        email="cu@example.com",
        username="cuuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    _, access_tok, _ = auth_services.issue_token_pair(db_session, user)
    db_session.refresh(user)

    resolved = auth_services.get_current_user(db_session, access_tok)
    assert resolved.id == user.id


# ===========================================================================
# U-A-14 — get_current_user: expired token → 401
# ===========================================================================
def test_get_current_user_expired_token_raises_401(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired access token must raise HTTP 401 Unauthorized."""
    user = User(
        id="exp-user-1",
        email="exp@example.com",
        username="expuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Build token that expires immediately in the past
    from datetime import UTC
    import helpers.jwt as jwt_mod

    past = datetime.now(UTC) - timedelta(minutes=5)
    payload = {
        "sub": user.id,
        "email": user.email,
        "username": user.username,
        "role": user.role,
        "token_type": "access",
        "token_version": user.token_version,
        "iat": int((past - timedelta(minutes=15)).timestamp()),
        "exp": int(past.timestamp()),
    }
    expired_token = jwt_mod._jwt_sign(payload, jwt_mod._auth_secret())

    with pytest.raises(HTTPException) as exc:
        auth_services.get_current_user(db_session, expired_token)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail.lower()


# ===========================================================================
# U-A-15 — get_current_user: revoked token_version → 401
# ===========================================================================
def test_get_current_user_revoked_token_version_raises_401(db_session: Session, fake_redis: FakeRedis) -> None:
    """An access token whose token_version no longer matches DB must raise 401."""
    user = User(
        id="rv-user-1",
        email="rv@example.com",
        username="rvuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    _, access_tok, _ = auth_services.issue_token_pair(db_session, user)
    db_session.refresh(user)

    # Revoke: bump token_version again
    user.token_version += 10
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        auth_services.get_current_user(db_session, access_tok)
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail.lower()


# ===========================================================================
# U-A-16 — verify_otp: correct OTP → user becomes verified
# ===========================================================================
def test_verify_otp_happy_path(db_session: Session) -> None:
    """Correct OTP must mark the user as verified and clear the challenge fields."""
    payload = RegisterRequest(email="otp-ok@example.com", username="otpok", password="password123")
    user = auth_services.create_user(db_session, payload)
    auth_services.request_otp(db_session, type("Req", (), {"email": user.email})())  # type: ignore[call-arg]
    db_session.refresh(user)

    result = auth_services.verify_otp(db_session, OtpVerifyRequest(email="otp-ok@example.com", otp_code=DEV_OTP))

    assert result.is_verified is True
    assert result.otp_code_hash is None
    assert result.otp_code_expires_at is None


# ===========================================================================
# U-A-17 — verify_otp: expired OTP → 400
# ===========================================================================
def test_verify_otp_expired_raises_400(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifying against an expired OTP must raise HTTP 400 Bad Request."""
    user = User(
        email="otp-exp@example.com",
        username="otpexp",
        password_hash="hash",
        auth_provider="local",
        is_verified=False,
    )
    user.otp_code_hash = auth_services.hash_secret(DEV_OTP)
    user.otp_code_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    with pytest.raises(HTTPException) as exc:
        auth_services.verify_otp(db_session, OtpVerifyRequest(email="otp-exp@example.com", otp_code=DEV_OTP))
    assert exc.value.status_code == 400
    assert "expired" in exc.value.detail.lower()


# ===========================================================================
# U-A-18 — verify_otp: wrong OTP code → 400
# ===========================================================================
def test_verify_otp_wrong_code_raises_400(db_session: Session) -> None:
    """Submitting a wrong OTP code must raise HTTP 400 Bad Request."""
    user = User(
        email="otp-bad@example.com",
        username="otpbad",
        password_hash="hash",
        auth_provider="local",
        is_verified=False,
    )
    user.otp_code_hash = auth_services.hash_secret(DEV_OTP)
    user.otp_code_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    user.otp_attempts = 0
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    with pytest.raises(HTTPException) as exc:
        auth_services.verify_otp(db_session, OtpVerifyRequest(email="otp-bad@example.com", otp_code="000000"))
    assert exc.value.status_code == 400
    assert "invalid" in exc.value.detail.lower()


# ===========================================================================
# U-A-19 — verify_otp: OTP attempts exceeded (≥ 5) → 429
# ===========================================================================
def test_verify_otp_attempts_exceeded_raises_429(db_session: Session) -> None:
    """When otp_attempts >= 5, further attempts must raise HTTP 429."""
    user = User(
        email="otp-rate@example.com",
        username="otprate",
        password_hash="hash",
        auth_provider="local",
        is_verified=False,
    )
    user.otp_code_hash = auth_services.hash_secret(DEV_OTP)
    user.otp_code_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    user.otp_attempts = 5  # already at limit
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    with pytest.raises(HTTPException) as exc:
        auth_services.verify_otp(db_session, OtpVerifyRequest(email="otp-rate@example.com", otp_code=DEV_OTP))
    assert exc.value.status_code == 429


# ===========================================================================
# U-A-20 — request_otp: unknown email → 404
# ===========================================================================
def test_request_otp_unknown_email_raises_404(db_session: Session) -> None:
    """Requesting an OTP for an unregistered email must raise HTTP 404."""
    from schemas.auth_schema import OtpRequest

    with pytest.raises(HTTPException) as exc:
        auth_services.request_otp(db_session, OtpRequest(email="ghost@example.com"))
    assert exc.value.status_code == 404


# ===========================================================================
# U-A-21 — request_password_reset: valid email returns token + expiry
# ===========================================================================
def test_request_password_reset_happy_path(db_session: Session) -> None:
    """A registered user's reset request must return a non-empty reset token."""
    user = User(email="reset@example.com", username="resetuser", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()

    result_user, reset_token, expires_at = auth_services.request_password_reset(
        db_session, PasswordResetRequest(email="reset@example.com")
    )

    assert reset_token and len(reset_token) > 16
    assert expires_at > datetime.now(UTC)
    assert result_user.password_reset_token_hash is not None


# ===========================================================================
# U-A-22 — request_password_reset: unknown email → 404
# ===========================================================================
def test_request_password_reset_unknown_email_raises_404(db_session: Session) -> None:
    """Password reset for an unregistered email must raise HTTP 404."""
    with pytest.raises(HTTPException) as exc:
        auth_services.request_password_reset(db_session, PasswordResetRequest(email="nobody@example.com"))
    assert exc.value.status_code == 404


# ===========================================================================
# U-A-23 — reset_password: valid token + new password → user updated
# ===========================================================================
def test_reset_password_happy_path(db_session: Session) -> None:
    """Valid reset token must allow updating the user's password."""
    user = User(email="resetok@example.com", username="resetok", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()

    _, reset_token, _ = auth_services.request_password_reset(db_session, PasswordResetRequest(email="resetok@example.com"))

    updated = auth_services.reset_password(
        db_session,
        PasswordResetConfirmRequest(email="resetok@example.com", reset_token=reset_token, new_password="newpassword1"),
    )

    assert updated.password_reset_token_hash is None
    assert updated.password_reset_expires_at is None
    assert jwt_helpers.verify_password("newpassword1", updated.password_hash)


# ===========================================================================
# U-A-24 — reset_password: expired reset token → 400
# ===========================================================================
def test_reset_password_expired_token_raises_400(db_session: Session) -> None:
    """An expired password reset token must raise HTTP 400 Bad Request."""
    import secrets as sec

    raw_token = sec.token_urlsafe(32)
    user = User(email="resetexp@example.com", username="resetexp", password_hash="hash", auth_provider="local", is_verified=True)
    user.password_reset_token_hash = auth_services.hash_secret(raw_token)
    user.password_reset_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    with pytest.raises(HTTPException) as exc:
        auth_services.reset_password(
            db_session,
            PasswordResetConfirmRequest(email="resetexp@example.com", reset_token=raw_token, new_password="newpassword1"),
        )
    assert exc.value.status_code == 400
    assert "expired" in exc.value.detail.lower()


# ===========================================================================
# U-A-25 — reset_password: invalid reset token → 400
# ===========================================================================
def test_reset_password_invalid_token_raises_400(db_session: Session) -> None:
    """A wrong reset token value must raise HTTP 400 Bad Request."""
    user = User(email="resetbad@example.com", username="resetbad", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()

    _, _, _ = auth_services.request_password_reset(db_session, PasswordResetRequest(email="resetbad@example.com"))

    with pytest.raises(HTTPException) as exc:
        auth_services.reset_password(
            db_session,
            PasswordResetConfirmRequest(email="resetbad@example.com", reset_token="wrong-token-value-xyz", new_password="newpass1234"),
        )
    assert exc.value.status_code == 400
    assert "invalid" in exc.value.detail.lower()


# ===========================================================================
# U-A-26 — revoke_session: clears Redis ZSET for user
# ===========================================================================
def test_revoke_session_clears_redis_entry(db_session: Session, fake_redis: FakeRedis) -> None:
    """After revoking a session, the specific sid must be gone from Redis."""
    user = User(
        id="rev-user-1",
        email="rev@example.com",
        username="revuser",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    _, _, refresh_tok = auth_services.issue_token_pair(db_session, user)
    db_session.refresh(user)
    zset_key = f"user:sessions:{user.id}"
    assert fake_redis.zcard(zset_key) > 0  # session exists

    auth_services.revoke_session(db_session, user, refresh_tok)
    assert fake_redis.zcard(zset_key) == 0  # session cleared


# ===========================================================================
# U-A-27 — hash_password / verify_password: round-trip
# ===========================================================================
def test_password_hash_round_trip() -> None:
    """Hashing then verifying a password should succeed; a wrong password should fail."""
    raw = "supersecretpassword"
    hashed = jwt_helpers.hash_password(raw)

    assert hashed != raw
    assert hashed.startswith("pbkdf2_sha256$")
    assert jwt_helpers.verify_password(raw, hashed) is True
    assert jwt_helpers.verify_password("wrongpassword", hashed) is False


# ===========================================================================
# U-A-28 — JWT decode: tampered signature → 401
# ===========================================================================
def test_jwt_tampered_signature_raises_401() -> None:
    """Modifying the JWT signature must raise HTTP 401 Unauthorized."""
    user = User(
        id="jwt-user-1",
        email="jwt@example.com",
        username="jwtuser",
        role="user",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    token = jwt_helpers.create_access_token(user)
    header, payload, sig = token.split(".")
    tampered = f"{header}.{payload}.invalidsignatureXXX"

    with pytest.raises(HTTPException) as exc:
        jwt_helpers.decode_access_token(tampered)
    assert exc.value.status_code == 401
    assert "signature" in exc.value.detail.lower() or "invalid" in exc.value.detail.lower()


# ===========================================================================
# U-A-29 — JWT decode: wrong token type → 401
# ===========================================================================
def test_jwt_wrong_token_type_raises_401() -> None:
    """Using a refresh token where an access token is expected must raise 401."""
    user = User(
        id="jt-user-1",
        email="jt@example.com",
        username="jtuser",
        role="user",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    refresh_tok = jwt_helpers.create_refresh_token(user, sid="any-sid")

    # Try to decode a refresh token as if it were an access token
    with pytest.raises(HTTPException) as exc:
        jwt_helpers.decode_access_token(refresh_tok)
    assert exc.value.status_code == 401
    assert "type" in exc.value.detail.lower() or "invalid" in exc.value.detail.lower()
