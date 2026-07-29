"""Unit tests for authentication service behavior."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from typing import Generator
from sqlalchemy.orm import Session

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from src.models.auth_model import Base, User
from src.schemas.auth_schema import LoginRequest, OtpVerifyRequest, RegisterRequest
from src.services import auth_services


class FakeRedis:
    """Small in-memory Redis stub for auth service tests."""

    def __init__(self) -> None:
        self.members: dict[tuple[str, str], float] = {}

    def zscore(self, key: str, member: str) -> float | None:
        return self.members.get((key, member))

    def zrem(self, key: str, member: str) -> None:
        self.members.pop((key, member), None)

    def delete(self, *keys: str) -> None:
        for key in keys:
            for member_key in list(self.members.keys()):
                if member_key[0] == key:
                    self.members.pop(member_key, None)


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Create an isolated SQLite-backed session for each test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        # ✅ Only the cleanup steps go here
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Patch the auth service to use an in-memory Redis stub."""
    redis_stub = FakeRedis()

    def fake_limiter(**_: object) -> None:
        key = f"user:sessions:1"
        redis_stub.members[(key, "sid-123")] = 1.0

    monkeypatch.setattr(auth_services, "redis_server", redis_stub)
    monkeypatch.setattr(auth_services, "active_session_limiter", fake_limiter)
    return redis_stub


def test_create_user_persists_new_user_and_rejects_duplicate_email(db_session: Session) -> None:
    """New users should persist and duplicate emails should be rejected."""
    payload = RegisterRequest(email="user@example.com", username="user", full_name="Example User", password="password123")

    user = auth_services.create_user(db_session, payload)

    assert user.email == "user@example.com"
    assert user.username == "user"

    with pytest.raises(HTTPException) as exc_info:
        auth_services.create_user(db_session, payload)

    assert exc_info.value.status_code == 409


def test_authenticate_user_rejects_invalid_credentials(db_session: Session) -> None:
    """Authentication should fail for unknown users and incorrect passwords."""
    payload = RegisterRequest(email="auth@example.com", username="auth", full_name="Auth User", password="password123")
    user = auth_services.create_user(db_session, payload)
    user.is_verified = True
    db_session.commit()

    invalid_login = LoginRequest(identifier="auth@example.com", password="wrong-password")
    with pytest.raises(HTTPException) as exc_info:
        auth_services.authenticate_user(db_session, invalid_login)

    assert exc_info.value.status_code == 401


def test_issue_token_pair_creates_access_and_refresh_tokens(db_session: Session, fake_redis: FakeRedis) -> None:
    """Issuing a token pair should create both a refresh session and signed tokens."""
    user = User(id="user-1", email="token@example.com", username="token", full_name="Token User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    token_pair, access_token, refresh_token = auth_services.issue_token_pair(db_session, user)

    assert token_pair.user.email == user.email
    assert access_token.startswith("ey")
    assert refresh_token.startswith("ey")
    assert fake_redis.members


def test_resolve_refresh_user_rejects_evicted_session(db_session: Session, fake_redis: FakeRedis) -> None:
    """Refresh tokens should be rejected when their Redis session entry is missing."""
    user = User(email="refresh@example.com", username="refresh", full_name="Refresh User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    refresh_token = auth_services.create_refresh_token(user, "sid-123")

    with pytest.raises(HTTPException) as exc_info:
        auth_services.resolve_refresh_user(db_session, refresh_token)

    assert exc_info.value.status_code == 401


def test_verify_otp_rejects_expired_challenge(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired OTP challenges should fail verification with a client error."""
    monkeypatch.setattr(auth_services, "_now", lambda: datetime.now())

    user = User(email="otp-expired@example.com", username="otp", full_name="OTP User", password_hash="hash", auth_provider="local", is_verified=False)
    user.otp_code_hash = auth_services.hash_secret("123456")
    user.otp_code_expires_at = datetime.now() - timedelta(minutes=1)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    with pytest.raises(HTTPException) as exc_info:
        auth_services.verify_otp(db_session, OtpVerifyRequest(email="otp-expired@example.com", otp_code="123456"))

    assert exc_info.value.status_code == 400
