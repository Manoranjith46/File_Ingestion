"""Business logic for registration, JWT sessions, OTP, reset password, and Google sign-in."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

# Relax oauthlib scope checks for Google OAuth canonical scope URLs
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from sqlalchemy import func
from sqlalchemy.orm import Session
import logging
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)

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
from helpers.crypto import encrypt_str, decrypt_str
from models.auth_model import User
from config.redis_server import server as redis_server, active_session_limiter
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
from utils.errors import (
    AccountNotVerifiedError,
    ConfigurationError,
    DuplicateEmailError,
    DuplicateUsernameError,
    GoogleTokenExchangeError,
    InvalidCredentialsError,
    InvalidOTPError,
    InvalidResetTokenError,
    InvalidTokenError,
    MicrosoftTokenExchangeError,
    NoActiveOTPChallengeError,
    NoActiveResetChallengeError,
    OTPExpiredError,
    OtpAttemptsExceededError,
    ResetTokenExpiredError,
    UnauthorizedAccessError,
    UserNotFoundError,
)


def _now() -> datetime:
    """Return the current UTC timestamp.

    Returns:
        datetime: The current timezone-aware UTC time.
    """
    return datetime.now(UTC)


def _ensure_utc(dt: datetime) -> datetime:
    """Return a timezone-aware UTC datetime, attaching UTC if the input is naive.

    SQLite and some other database drivers strip timezone information when
    storing ``DateTime`` columns, so stored values may be read back as
    offset-naive datetimes.  This helper normalises them so comparisons
    against ``_now()`` (which is always UTC-aware) do not raise
    ``TypeError: can't compare offset-naive and offset-aware datetimes``.

    Args:
        dt: The datetime to normalise.

    Returns:
        datetime: A UTC-aware copy of the input.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


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

    raise ConfigurationError(message="Google client ID is not configured")


def _google_client_secret() -> str:
    """Return the configured Google OAuth client secret.

    Returns:
        str: The configured Google OAuth client secret.
    """
    client_secret = get_env("GOOGLE_CLIENT_SECRET", default="", required=False).strip()
    if not client_secret:
        raise ConfigurationError(message="Google client secret is not configured")
    return client_secret


def _google_redirect_uri() -> str:
    """Return the configured Google redirect URI.

    Returns:
        str: The callback URI used for Google OAuth.
    """
    redirect_uri = get_env("GOOGLE_REDIRECT_URI", default="http://localhost:8000/auth/google/callback", required=False).strip()
    if not redirect_uri:
        raise ConfigurationError(message="Google redirect URI is not configured")
    return redirect_uri


def _frontend_url() -> str:
    """Return the frontend base URL.

    Returns:
        str: The configured frontend URL.
    """
    frontend_url = get_env("FRONTEND_URL", default="http://localhost:5173", required=False).strip()
    if not frontend_url:
        raise ConfigurationError(message="Frontend URL is not configured")
    return frontend_url


def _google_scopes() -> list[str]:
    """Return the scopes used by the Google login flow.

    Returns:
        list[str]: OAuth scopes for login and Drive file access.
    """
    return [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/drive.readonly",
    ]


def _google_flow(state: str | None = None, redirect_uri: str | None = None) -> Flow:
    """Build a Google OAuth flow instance.

    Args:
        state: Optional CSRF state value.
        redirect_uri: Optional override for OAuth callback redirect URI.

    Returns:
        Flow: A configured Google OAuth flow.
    """
    chosen_redirect_uri = redirect_uri or _google_redirect_uri()
    client_config = {
        "web": {
            "client_id": _google_client_id(),
            "client_secret": _google_client_secret(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [chosen_redirect_uri],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=_google_scopes(),
        state=state,
        redirect_uri=chosen_redirect_uri,
        autogenerate_code_verifier=False,
    )


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
        raise DuplicateEmailError()
    if username and db.query(User).filter(User.username == username).first() is not None:
        raise DuplicateUsernameError()

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
        raise InvalidCredentialsError()
    if not user.is_verified:
        raise AccountNotVerifiedError()

    user.last_login_at = _now()
    db.commit()
    db.refresh(user)
    return user


def issue_token_pair(db: Session, user: User) -> tuple[TokenPairResponse, str, str]:
    """
    Rotate the token version and return a safe auth payload, access token, and refresh token.

    Args:
        db (Session): The active database session.
        user (User): The user object to issue tokens for.

    Returns:
        tuple[TokenPairResponse, str, str]: A tuple containing TokenPairResponse, access_token, and refresh_token.
    """
    user.token_version += 1
    user.last_login_at = _now()
    db.commit()
    db.refresh(user)
    access_token = create_access_token(user)

    # Enforce active session limit using Redis Lua script
    sid = secrets.token_hex(16)
    zset_key = f"user:sessions:{user.id}"
    now_timestamp = int(_now().timestamp())
    ttl_seconds = int(_refresh_token_ttl().total_seconds())
    max_sessions = int(get_env("MAX_ACTIVE_SESSIONS", default="3", required=False))

    active_session_limiter(keys=[zset_key], args=[now_timestamp, sid, max_sessions, ttl_seconds])

    refresh_token = create_refresh_token(user, sid)
    return TokenPairResponse(user=create_public_user(user), access_token=access_token), access_token, refresh_token


def resolve_refresh_user(db: Session, refresh_token: str) -> User:
    """
    Resolve the current user from a refresh token and check session state.

    Args:
        db (Session): The active database session.
        refresh_token (str): The refresh token to verify.

    Returns:
        User: The user represented by the token.

    Raises:
        HTTPException: If the token is invalid, revoked, or the session was evicted.
    """
    payload = decode_refresh_token(refresh_token)
    user = db.query(User).filter(User.id == payload["sub"]).one_or_none()
    if user is None:
        raise UnauthorizedAccessError(message="User not found")
    if user.token_version != payload.get("token_version"):
        raise UnauthorizedAccessError(message="Refresh token has been revoked")

    # Validate session ID (sid) in Redis ZSET
    sid = payload.get("sid")
    if sid:
        zset_key = f"user:sessions:{user.id}"
        score = redis_server.zscore(zset_key, sid)
        if score is None:
            raise UnauthorizedAccessError(message="Session has been evicted or invalidated")

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
        raise UnauthorizedAccessError(message="User not found")
    if user.token_version != payload.get("token_version"):
        raise UnauthorizedAccessError(message="Token has been revoked")
    return user


def refresh_session(db: Session, refresh_token: str) -> tuple[TokenPairResponse, str, str]:
    """
    Validate a refresh token and issue a new token pair.

    Args:
        db (Session): The active database session.
        refresh_token (str): The refresh token to verify.

    Returns:
        tuple[TokenPairResponse, str, str]: A tuple containing TokenPairResponse, access_token, and refresh_token.
    """
    user = resolve_refresh_user(db, refresh_token)
    return issue_token_pair(db, user)


def revoke_session(db: Session, user: User, refresh_token: str | None = None) -> None:
    """
    Invalidate active tokens for a user, clearing Redis session list.

    Args:
        db (Session): The active database session.
        user (User): The user whose sessions should be revoked.
        refresh_token (str | None): Optional specific refresh token to revoke.

    Returns:
        None
    """
    user.token_version += 1
    db.commit()

    zset_key = f"user:sessions:{user.id}"
    if refresh_token:
        try:
            payload = decode_refresh_token(refresh_token)
            sid = payload.get("sid")
            if sid:
                redis_server.zrem(zset_key, sid)
        except Exception:
            redis_server.delete(zset_key)
    else:
        redis_server.delete(zset_key)


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
        raise UserNotFoundError()

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
        raise UserNotFoundError()
    if user.otp_code_hash is None or user.otp_code_expires_at is None:
        raise NoActiveOTPChallengeError()
    if _ensure_utc(user.otp_code_expires_at) <= _now():
        raise OTPExpiredError()
    if user.otp_attempts >= 5:
        raise OtpAttemptsExceededError()

    user.otp_attempts += 1
    if not verify_secret(payload.otp_code, user.otp_code_hash):
        db.commit()
        raise InvalidOTPError()

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
        raise UserNotFoundError()

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
        raise UserNotFoundError()
    if user.password_reset_token_hash is None or user.password_reset_expires_at is None:
        raise NoActiveResetChallengeError()
    if _ensure_utc(user.password_reset_expires_at) <= _now():
        raise ResetTokenExpiredError()
    if not verify_secret(payload.reset_token, user.password_reset_token_hash):
        raise InvalidResetTokenError()

    user.password_hash = hash_password(payload.new_password)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return user


def continue_with_google(db: Session, code: str, redirect_uri: str | None = None) -> tuple[User, bool]:
    """Exchange a Google authorization code and create or link a local user.

    Args:
        db: The active database session.
        code: The Google authorization code.
        redirect_uri: Optional override for callback redirect URI matching the request.

    Returns:
        tuple[User, bool]: The linked or newly created user account and whether it is new.

    Raises:
        HTTPException: If Google auth is misconfigured or the token is invalid.
    """
    try:
        flow = _google_flow(redirect_uri=redirect_uri)
        flow.fetch_token(code=code)
    except Exception as err:
        logger.error(f"Fetch token with redirect_uri ({redirect_uri}) failed: {err}")
        raise GoogleTokenExchangeError(message=f"Google token exchange failed: {err}")

    credentials = flow.credentials
    if not credentials.id_token:
        raise GoogleTokenExchangeError(message="Google token exchange did not return an ID token")

    google_claims = id_token.verify_oauth2_token(
        credentials.id_token,
        Request(),
        audience=_google_client_id(),
        clock_skew_in_seconds=10,
    )
    google_subject = google_claims.get("sub")
    email = google_claims.get("email")
    full_name = google_claims.get("name")
    email_verified = bool(google_claims.get("email_verified"))

    if not google_subject or not email:
        raise GoogleTokenExchangeError(message="Google token is missing account data")
    if not email_verified:
        raise GoogleTokenExchangeError(message="Google account email is not verified")

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
            google_refresh_token=encrypt_str(credentials.refresh_token) if credentials.refresh_token else None,
            is_verified=True,
            token_version=1,
        )
        db.add(user)
    else:
        user.google_subject = google_subject
        user.auth_provider = "google"
        user.is_verified = True
        if credentials.refresh_token:
            user.google_refresh_token = encrypt_str(credentials.refresh_token)
        if full_name and not user.full_name:
            user.full_name = full_name

    user.last_login_at = _now()
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    db.refresh(user)
    return user, is_new_user


def build_google_frontend_redirect_url(is_new_user: bool) -> str:
    """Build the frontend redirect URL after Google login."""
    query = urlencode({"status": "success", "is_new_user": str(is_new_user).lower()})
    return f"{_frontend_url().rstrip('/')}/auth/google/callback?{query}"


def _microsoft_client_id() -> str:
    """Return the configured Microsoft OAuth client ID."""
    client_id = get_env("MICROSOFT_CLIENT_ID", default="", required=False).strip()
    if not client_id:
        raise ConfigurationError(message="Microsoft client ID is not configured")
    return client_id


def _microsoft_client_secret() -> str:
    """Return the configured Microsoft OAuth client secret."""
    client_secret = get_env("MICROSOFT_CLIENT_SECRET", default="", required=False).strip()
    if not client_secret:
        raise ConfigurationError(message="Microsoft client secret is not configured")
    return client_secret


def _microsoft_tenant_id() -> str:
    """Return the configured Microsoft tenant ID."""
    return get_env("MICROSOFT_TENANT_ID", default="common", required=False).strip() or "common"


def _microsoft_redirect_uri() -> str:
    """Return the configured Microsoft redirect URI."""
    return get_env("MICROSOFT_REDIRECT_URI", default="http://localhost:8000/v1/auth/microsoft/callback", required=False).strip()


def _microsoft_scopes() -> str:
    """Return space-separated Microsoft Graph API scopes."""
    return "offline_access User.Read Files.Read.All Sites.Read.All"


def build_microsoft_login_url() -> tuple[str, str]:
    """Create the Microsoft authorization URL and state value.

    Returns:
        tuple[str, str]: The authorization URL and CSRF state.
    """
    state = secrets.token_urlsafe(24)
    tenant = _microsoft_tenant_id()
    params = {
        "client_id": _microsoft_client_id(),
        "response_type": "code",
        "redirect_uri": _microsoft_redirect_uri(),
        "response_mode": "query",
        "scope": _microsoft_scopes(),
        "state": state,
        "prompt": "consent",
    }
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{urlencode(params)}"
    return url, state


def continue_with_microsoft(db: Session, code: str, redirect_uri: str | None = None) -> tuple[User, bool]:
    """Exchange a Microsoft authorization code for tokens and link or create user.

    Args:
        db: Active database session.
        code: Authorization code from Microsoft.
        redirect_uri: Optional override redirect URI.

    Returns:
        tuple[User, bool]: Linked user and whether newly created.
    """
    import requests

    tenant = _microsoft_tenant_id()
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    chosen_redirect_uri = redirect_uri or _microsoft_redirect_uri()

    payload = {
        "client_id": _microsoft_client_id(),
        "client_secret": _microsoft_client_secret(),
        "code": code,
        "redirect_uri": chosen_redirect_uri,
        "grant_type": "authorization_code",
        "scope": _microsoft_scopes(),
    }

    resp = requests.post(token_url, data=payload, timeout=30)
    if resp.status_code != 200:
        logger.error(f"Microsoft token exchange failed: {resp.text}")
        raise MicrosoftTokenExchangeError(message=f"Microsoft token exchange failed: {resp.text}")

    token_data = resp.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        raise MicrosoftTokenExchangeError(message="Microsoft token exchange did not return an access token")

    # Fetch Microsoft User Profile
    profile_resp = requests.get(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if profile_resp.status_code != 200:
        logger.error(f"Microsoft user profile fetch failed: {profile_resp.text}")
        raise MicrosoftTokenExchangeError(message="Failed to fetch Microsoft user profile")

    profile = profile_resp.json()
    email = profile.get("mail") or profile.get("userPrincipalName")
    full_name = profile.get("displayName")

    if not email:
        raise MicrosoftTokenExchangeError(message="Microsoft account profile is missing email")

    user = db.query(User).filter((func.lower(User.email) == email.lower())).one_or_none()
    is_new_user = user is None

    if user is None:
        user = User(
            email=email,
            username=email.split("@")[0],
            full_name=full_name,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            auth_provider="microsoft",
            microsoft_refresh_token=encrypt_str(refresh_token) if refresh_token else None,
            is_verified=True,
            token_version=1,
        )
        db.add(user)
    else:
        user.auth_provider = "microsoft"
        user.is_verified = True
        if refresh_token:
            user.microsoft_refresh_token = encrypt_str(refresh_token)
        if full_name and not user.full_name:
            user.full_name = full_name

    user.last_login_at = _now()
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    db.refresh(user)
    return user, is_new_user


def build_microsoft_frontend_redirect_url(is_new_user: bool) -> str:
    """Build the frontend redirect URL after Microsoft login."""
    query = urlencode({"status": "success", "is_new_user": str(is_new_user).lower()})
    return f"{_frontend_url().rstrip('/')}/auth/microsoft/callback?{query}"