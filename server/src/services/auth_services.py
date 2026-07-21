"""Business logic for registration, JWT sessions, OTP, reset password, and Google sign-in."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow

from helpers.get_env import get_env
from helpers.jwt import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    generate_otp_code,
    hash_password,
    hash_secret,
    verify_password,
    verify_secret,
)
from models.auth_model import User
from schemas.auth_schema import (
    LoginRequest,
    OtpRequest,
    OtpVerifyRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    PublicUserSchema,
    RegisterRequest,
    TokenPairResponse,
)


def _now() -> datetime:
    """Return the current UTC timestamp.

    Returns:
        datetime: The current timezone-aware UTC time.
    """
    return datetime.now(UTC)


def _access_token_ttl() -> timedelta:
    """Return the configured access-token lifetime.

    Returns:
        timedelta: The access-token lifetime.
    """
    return timedelta(minutes=int(get_env("ACCESS_TOKEN_EXPIRE_MINUTES", default="15", required=False)))


def _refresh_token_ttl() -> timedelta:
    """Return the configured refresh-token lifetime.

    Returns:
        timedelta: The refresh-token lifetime.
    """
    return timedelta(days=int(get_env("REFRESH_TOKEN_EXPIRE_DAYS", default="7", required=False)))


def _otp_token_ttl() -> timedelta:
    """Return the configured OTP lifetime.

    Returns:
        timedelta: The OTP lifetime.
    """
    return timedelta(minutes=int(get_env("OTP_EXPIRE_MINUTES", default="10", required=False)))


def _password_reset_ttl() -> timedelta:
    """Return the configured password-reset lifetime.

    Returns:
        timedelta: The reset-token lifetime.
    """
    return timedelta(minutes=int(get_env("PASSWORD_RESET_EXPIRE_MINUTES", default="15", required=False)))


def _google_client_id() -> str:
    """Return the configured Google OAuth client ID.

    Returns:
        str: The configured Google OAuth client ID.
    """
    client_id = get_env("GOOGLE_CLIENT_ID", default="", required=False).strip()
    if client_id:
        return client_id

    configured_ids = get_env("GOOGLE_CLIENT_IDS", default="", required=False)
    if configured_ids:
        first_client_id = next((client_id.strip() for client_id in configured_ids.split(",") if client_id.strip()), "")
        if first_client_id:
            return first_client_id

    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google client ID is not configured")


def _google_client_secret() -> str:
    """Return the configured Google OAuth client secret.

    Returns:
        str: The configured Google OAuth client secret.
    """
    client_secret = get_env("GOOGLE_CLIENT_SECRET", default="", required=False).strip()
    if not client_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google client secret is not configured")
    return client_secret


def _google_redirect_uri() -> str:
    """Return the configured Google redirect URI.

    Returns:
        str: The callback URI used for Google OAuth.
    """
    redirect_uri = get_env("GOOGLE_REDIRECT_URI", default="http://localhost:8000/auth/google/callback", required=False).strip()
    if not redirect_uri:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google redirect URI is not configured")
    return redirect_uri


def _frontend_url() -> str:
    """Return the frontend base URL.

    Returns:
        str: The configured frontend URL.
    """
    frontend_url = get_env("FRONTEND_URL", default="http://localhost:5173", required=False).strip()
    if not frontend_url:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Frontend URL is not configured")
    return frontend_url


def _google_scopes() -> list[str]:
    """Return the scopes used by the Google login flow.

    Returns:
        list[str]: OAuth scopes for login.
    """
    return ["openid", "email", "profile"]


def _google_flow(state: str | None = None) -> Flow:
    """Build a Google OAuth flow instance.

    Args:
        state: Optional CSRF state value.

    Returns:
        Flow: A configured Google OAuth flow.
    """
    client_config = {
        "web": {
            "client_id": _google_client_id(),
            "client_secret": _google_client_secret(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [_google_redirect_uri()],
        }
    }
    return Flow.from_client_config(client_config, scopes=_google_scopes(), state=state, redirect_uri=_google_redirect_uri())


def build_google_login_url() -> tuple[str, str]:
    """Create the Google authorization URL and state value.

    Returns:
        tuple[str, str]: The authorization URL and CSRF state.
    """
    flow = _google_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return authorization_url, state


def create_public_user(user: User) -> PublicUserSchema:
    """Convert a SQLAlchemy user row into a public response schema.

    Args:
        user: The persisted user record.

    Returns:
        PublicUserSchema: The public representation of the user.
    """
    return PublicUserSchema.model_validate(user)


def _find_user_by_identifier(db: Session, identifier: str) -> User | None:
    """Look up a user by username or email.

    Args:
        db: The active database session.
        identifier: A username or email address.

    Returns:
        User | None: The matching user record when found.
    """
    ident = identifier.strip()
    if "@" in ident:
        return db.query(User).filter(func.lower(User.email) == ident.lower()).one_or_none()
    return db.query(User).filter((User.username == ident) | (func.lower(User.email) == ident.lower())).one_or_none()


def create_user(db: Session, payload: RegisterRequest) -> User:
    """Create a local user account and persist it to the database.

    Args:
        db: The active database session.
        payload: The validated registration payload.

    Returns:
        User: The newly created user record.

    Raises:
        HTTPException: If the email or username already exists.
    """
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
    """Validate a local user login request.

    Args:
        db: The active database session.
        payload: The validated login payload.

    Returns:
        User: The authenticated user.

    Raises:
        HTTPException: If the credentials are invalid or the account is not verified.
    """
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
    """Rotate the token version and return a fresh access token response.

    Args:
        db: The active database session.
        user: The authenticated user.

    Returns:
        TokenPairResponse: The new access token payload and refresh expiry.
    """
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
    """Resolve the current user from a refresh token.

    Args:
        db: The active database session.
        refresh_token: The refresh token to verify.

    Returns:
        User: The user represented by the token.

    Raises:
        HTTPException: If the token is invalid, revoked, or the user is missing.
    """
    payload = decode_refresh_token(refresh_token)
    user = db.query(User).filter(User.id == payload["sub"]).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.token_version != payload.get("token_version"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token has been revoked")
    return user


def get_current_user(db: Session, access_token: str) -> User:
    """Resolve the current user from an access token.

    Args:
        db: The active database session.
        access_token: The access token to verify.

    Returns:
        User: The user represented by the token.

    Raises:
        HTTPException: If the token is invalid, revoked, or the user is missing.
    """
    payload = decode_access_token(access_token)
    user = db.query(User).filter(User.id == payload["sub"]).one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if user.token_version != payload.get("token_version"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")
    return user


def refresh_session(db: Session, refresh_token: str) -> TokenPairResponse:
    """Validate a refresh token and issue a new token pair.

    Args:
        db: The active database session.
        refresh_token: The refresh token to verify.

    Returns:
        TokenPairResponse: The refreshed token response.
    """
    user = resolve_refresh_user(db, refresh_token)
    return issue_token_pair(db, user)


def revoke_session(db: Session, user: User) -> None:
    """Invalidate all active tokens for a user.

    Args:
        db: The active database session.
        user: The user whose sessions should be revoked.
    """
    user.token_version += 1
    db.commit()


def request_otp(db: Session, payload: OtpRequest) -> tuple[User, str, datetime]:
    """Create a new OTP challenge for an account.

    Args:
        db: The active database session.
        payload: The email address to challenge.

    Returns:
        tuple[User, str, datetime]: The user, OTP code, and expiration time.

    Raises:
        HTTPException: If the user does not exist.
    """
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
    print(f"OTP for {user.email}: {otp_code}")

    db.refresh(user)
    return user, otp_code, expires_at


def verify_otp(db: Session, payload: OtpVerifyRequest) -> User:
    """Validate an OTP and mark the user as verified.

    Args:
        db: The active database session.
        payload: The OTP verification payload.

    Returns:
        User: The verified user.

    Raises:
        HTTPException: If the OTP is missing, expired, or invalid.
    """
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
    """Create a password reset challenge and return the reset token.

    Args:
        db: The active database session.
        payload: The email address to challenge.

    Returns:
        tuple[User, str, datetime]: The user, reset token, and expiration time.

    Raises:
        HTTPException: If the user does not exist.
    """
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
    """Validate a password reset token and set a new password.

    Args:
        db: The active database session.
        payload: The password reset payload.

    Returns:
        User: The user with the updated password.

    Raises:
        HTTPException: If the reset token is invalid or expired.
    """
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


def continue_with_google(db: Session, code: str) -> tuple[User, bool]:
    """Exchange a Google authorization code and create or link a local user.

    Args:
        db: The active database session.
        code: The Google authorization code.

    Returns:
        tuple[User, bool]: The linked or newly created user account and whether it is new.

    Raises:
        HTTPException: If Google auth is misconfigured or the token is invalid.
    """
    flow = _google_flow()
    flow.fetch_token(code=code)
    credentials = flow.credentials
    if not credentials.id_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google token exchange did not return an ID token")

    google_claims = id_token.verify_oauth2_token(credentials.id_token, Request(), audience=_google_client_id())
    google_subject = google_claims.get("sub")
    email = google_claims.get("email")
    full_name = google_claims.get("name")
    email_verified = bool(google_claims.get("email_verified"))

    if not google_subject or not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google token is missing account data")
    if not email_verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google account email is not verified")

    user = db.query(User).filter((User.google_subject == google_subject) | (func.lower(User.email) == (email or "").lower())).one_or_none()
    is_new_user = user is None
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
    return user, is_new_user


def build_google_frontend_redirect_url(access_token: str, is_new_user: bool) -> str:
    """Build the frontend redirect URL after Google login.

    Args:
        access_token: The application access token.
        is_new_user: Whether the Google account created a new local user.

    Returns:
        str: The frontend redirect URL with a URL fragment payload.
    """
    fragment = urlencode(
        {
            "access_token": access_token,
            "token_type": "bearer",
            "is_new_user": str(is_new_user).lower(),
        }
    )
    return f"{_frontend_url().rstrip('/')}/auth/google/callback#{fragment}"