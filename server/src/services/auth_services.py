"""Business logic for registration, JWT sessions, OTP, reset password, and Google sign-in."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from helpers.GetEnv import get_env
from models.auth_model import User
from schemas.auth_schema import ( GoogleSignInRequest, LoginRequest, OtpRequest, OtpVerifyRequest, PasswordResetConfirmRequest, PasswordResetRequest, PublicUserSchema, RegisterRequest, TokenPairResponse )
from helpers.jwt import ( create_access_token, create_refresh_token, decode_access_token, decode_refresh_token, generate_otp_code, hash_password, hash_secret, verify_password, verify_secret )


def _now() -> datetime:
    return datetime.now(UTC)


def _access_token_ttl() -> timedelta:
    return timedelta(minutes=int(get_env("ACCESS_TOKEN_EXPIRE_MINUTES", default="15", required=False)))


def _refresh_token_ttl() -> timedelta:
    return timedelta(days=int(get_env("REFRESH_TOKEN_EXPIRE_DAYS", default="7", required=False)))


def _otp_token_ttl() -> timedelta:
    return timedelta(minutes=int(get_env("OTP_EXPIRE_MINUTES", default="10", required=False)))


def _password_reset_ttl() -> timedelta:
    return timedelta(minutes=int(get_env("PASSWORD_RESET_EXPIRE_MINUTES", default="15", required=False)))


def _google_client_ids() -> list[str]:
    configured_ids = get_env("GOOGLE_CLIENT_IDS", default="", required=False)
    if not configured_ids:
        single_client_id = get_env("GOOGLE_CLIENT_ID", default="", required=False)
        if single_client_id:
            return [single_client_id]
        return []
    return [client_id.strip() for client_id in configured_ids.split(",") if client_id.strip()]


def create_public_user(user: User) -> PublicUserSchema:
    """Convert a SQLAlchemy user row into a public response schema."""
    return PublicUserSchema.model_validate(user)


def _find_user_by_identifier(db: Session, identifier: str) -> User | None:
    ident = identifier.strip()
    if "@" in ident:
        return (
            db.query(User)
            .filter(func.lower(User.email) == ident.lower())
            .one_or_none()
        )
    return (
        db.query(User)
        .filter((User.username == ident) | (func.lower(User.email) == ident.lower()))
        .one_or_none()
    )


def create_user(db: Session, payload: RegisterRequest) -> User:
    """Create a local user account and persist it to the database."""
    # normalize inputs: strip whitespace and normalize email to lowercase
    email = payload.email.strip().lower() if isinstance(payload.email, str) else payload.email
    username = payload.username.strip() if payload.username else None

    if db.query(User).filter(User.email == email).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")
    if username and db.query(User).filter(User.username == username).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
    user = User(
        email=email,
        username=username,
        full_name=payload.full_name.strip() if payload.full_name else None,
        password_hash=hash_password(payload.password),
        auth_provider="local",
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, payload: LoginRequest) -> User:
    """Validate a local user login request and return the matching user."""
    user = _find_user_by_identifier(db, payload.identifier)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid login credentials")
    if not user.is_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is not verified")
    user.last_login_at = _now()
    db.commit()
    db.refresh(user)
    return user


def issue_token_pair(db: Session, user: User) -> TokenPairResponse:
    """Rotate the token version and return a fresh access/refresh token pair."""
    user.token_version += 1
    user.last_login_at = _now()
    db.commit()
    db.refresh(user)
    return TokenPairResponse(
        access_token=create_access_token(user),
        refresh_expires_at=_now() + _refresh_token_ttl(),
        user=create_public_user(user),
    )



def resolve_refresh_user(db: Session, refresh_token: str) -> User:
    """Resolve the current user from a refresh token without issuing a new session."""
    payload = decode_refresh_token(refresh_token)
    user = db.query(User).filter(User.id == payload["sub"]).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.token_version != payload.get("token_version"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token has been revoked")
    return user

def get_current_user(db: Session, access_token: str) -> User:
    """Resolve the current user from an access token."""
    payload = decode_access_token(access_token)
    user = db.query(User).filter(User.id == payload["sub"]).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.token_version != payload.get("token_version"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")
    return user


def refresh_session(db: Session, refresh_token: str) -> TokenPairResponse:
    """Validate a refresh token and issue a new token pair."""
    user = resolve_refresh_user(db, refresh_token)
    return issue_token_pair(db, user)


def revoke_session(db: Session, user: User) -> None:
    """Invalidate all active tokens for a user by rotating the token version."""
    user.token_version += 1
    db.commit()


def request_otp(db: Session, payload: OtpRequest) -> tuple[User, str, datetime]:
    """Create a new OTP challenge for an account."""
    email = payload.email.strip().lower() if isinstance(payload.email, str) else payload.email
    user = db.query(User).filter(func.lower(User.email) == email).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    otp_code = generate_otp_code()
    expires_at = _now() + _otp_token_ttl()
    user.otp_code_hash = hash_secret(otp_code)
    user.otp_code_expires_at = expires_at
    user.otp_attempts = 0
    db.commit()
    db.refresh(user)
    return user, otp_code, expires_at


def verify_otp(db: Session, payload: OtpVerifyRequest) -> User:
    """Validate an OTP and mark the user as verified."""
    email = payload.email.strip().lower() if isinstance(payload.email, str) else payload.email
    user = db.query(User).filter(func.lower(User.email) == email).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.otp_code_hash is None or user.otp_code_expires_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active OTP challenge")
    if user.otp_code_expires_at <= _now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP has expired")
    if user.otp_attempts >= 5:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="OTP attempts exceeded")

    user.otp_attempts += 1
    if not verify_secret(payload.otp_code, user.otp_code_hash):
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP code")

    user.is_verified = True
    user.otp_code_hash = None
    user.otp_code_expires_at = None
    user.otp_attempts = 0
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return user


def request_password_reset(db: Session, payload: PasswordResetRequest) -> tuple[User, str, datetime]:
    """Create a password reset challenge and return the reset token."""
    email = payload.email.strip().lower() if isinstance(payload.email, str) else payload.email
    user = db.query(User).filter(func.lower(User.email) == email).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    reset_token = secrets.token_urlsafe(32)
    expires_at = _now() + _password_reset_ttl()
    user.password_reset_token_hash = hash_secret(reset_token)
    user.password_reset_expires_at = expires_at
    db.commit()
    db.refresh(user)
    return user, reset_token, expires_at


def reset_password(db: Session, payload: PasswordResetConfirmRequest) -> User:
    """Validate a password reset token and set a new password."""
    email = payload.email.strip().lower() if isinstance(payload.email, str) else payload.email
    user = db.query(User).filter(func.lower(User.email) == email).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.password_reset_token_hash is None or user.password_reset_expires_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No active reset challenge")
    if user.password_reset_expires_at <= _now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset token has expired")
    if not verify_secret(payload.reset_token, user.password_reset_token_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid reset token")

    user.password_hash = hash_password(payload.new_password)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return user


def continue_with_google(db: Session, payload: GoogleSignInRequest) -> User:
    """Verify a Google ID token and create or link a local user."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token
    except ImportError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="google-auth is required for Google sign-in",
        ) from error

    client_ids = _google_client_ids()
    if not client_ids:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google client IDs are not configured")

    google_claims = id_token.verify_oauth2_token(payload.id_token, Request(), audience=client_ids)
    google_subject = google_claims.get("sub")
    email = google_claims.get("email")
    full_name = google_claims.get("name")
    email_verified = bool(google_claims.get("email_verified"))

    if not google_subject or not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google token is missing account data")
    if not email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google account email is not verified")

    user = db.query(User).filter((User.google_subject == google_subject) | (func.lower(User.email) == (email or "").lower())).one_or_none()
    if user is None:
        user = User(
            email=email,
            username=email.split("@")[0],
            full_name=full_name,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            auth_provider="google",
            google_subject=google_subject,
            is_verified=True,
        )
        db.add(user)
    else:
        user.google_subject = google_subject
        user.auth_provider = "google"
        user.is_verified = True
        if full_name and not user.full_name:
            user.full_name = full_name

    user.last_login_at = _now()
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return user