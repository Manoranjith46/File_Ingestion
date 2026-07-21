"""Authentication routes for registration, verification, session, and identity flows."""

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from config.database import get_db
from schemas.auth_schema import (
    LoginRequest,
    MessageResponse,
    OtpChallengeResponse,
    OtpRequest,
    OtpVerifyRequest,
    PasswordResetChallengeResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    PublicUserSchema,
    RegisterRequest,
    RegistrationResponse,
    TokenPairResponse,
)
from services.auth_services import (
    authenticate_user,
    create_public_user,
    create_refresh_token,
    build_google_frontend_redirect_url,
    build_google_login_url,
    continue_with_google,
    create_user,
    get_current_user,
    issue_token_pair,
    request_otp,
    request_password_reset,
    revoke_session,
    reset_password,
    resolve_refresh_user,
    verify_otp,
)


auth_router = APIRouter()


@auth_router.post("/register", response_model=RegistrationResponse)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """
        Register a new local user and create the first OTP challenge.
    """
    user = create_user(db, payload)
    try:
        user, otp_code, otp_expires_at = request_otp(db, OtpRequest(email=user.email))
    except HTTPException:
        db.delete(user)
        db.commit()
        raise
    return RegistrationResponse(user=create_public_user(user), otp_code=otp_code, otp_expires_at=otp_expires_at)


@auth_router.post("/login", response_model=TokenPairResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """
        Authenticate a verified local user and return a new token pair.
    """
    user = authenticate_user(db, payload)
    session = issue_token_pair(db, user)
    response.set_cookie(
        "refresh_token",
        create_refresh_token(user),
        httponly=True,
        secure=False,
        samesite="lax",
        path="/auth",
    )
    return session


@auth_router.post("/logout", response_model=MessageResponse)
def logout(
    response: Response,
    authorization: str | None = Header(default=None),
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """
        Invalidate the current user session and clear the refresh token cookie.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")
    access_token = authorization.removeprefix("Bearer ").strip()
    user = get_current_user(db, access_token)
    revoke_session(db, user)
    if refresh_token is not None:
        response.delete_cookie("refresh_token", path="/auth")
    return MessageResponse(message="Logged out successfully")


@auth_router.post("/refresh", response_model=TokenPairResponse)
def refresh(response: Response, refresh_token: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    """
        Rotate a refresh token and return a fresh access token pair.
    """
    if refresh_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")
    user = resolve_refresh_user(db, refresh_token)
    session = issue_token_pair(db, user)
    response.set_cookie(
        "refresh_token",
        create_refresh_token(user),
        httponly=True,
        secure=False,
        samesite="lax",
        path="/auth",
    )
    return session


@auth_router.get("/me", response_model=PublicUserSchema)
def me(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    """
        Return the authenticated user's public profile.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")
    access_token = authorization.removeprefix("Bearer ").strip()
    user = get_current_user(db, access_token)
    return create_public_user(user)


@auth_router.post("/request-otp", response_model=OtpChallengeResponse)
def request_otp_endpoint(payload: OtpRequest, db: Session = Depends(get_db)):
    """
        Create a new OTP challenge for the supplied email address.
    """
    user, otp_code, otp_expires_at = request_otp(db, payload)
    return OtpChallengeResponse(email=user.email, otp_code=otp_code, otp_expires_at=otp_expires_at)


@auth_router.post("/verify-otp", response_model=TokenPairResponse)
def verify_otp_endpoint(payload: OtpVerifyRequest, response: Response, db: Session = Depends(get_db)):
    """
        Verify the submitted OTP and establish a verified login session.
    """
    user = verify_otp(db, payload)
    session = issue_token_pair(db, user)
    response.set_cookie(
        "refresh_token",
        create_refresh_token(user),
        httponly=True,
        secure=False,
        samesite="lax",
        path="/auth",
    )
    return session


@auth_router.post("/request-password-reset", response_model=PasswordResetChallengeResponse)
def request_password_reset_endpoint(payload: PasswordResetRequest, db: Session = Depends(get_db)):
    """
        Create a password reset challenge for the supplied email address.
    """
    user, reset_token, reset_expires_at = request_password_reset(db, payload)
    return PasswordResetChallengeResponse(email=user.email, reset_token=reset_token, reset_expires_at=reset_expires_at)


@auth_router.post("/reset-password", response_model=MessageResponse)
def reset_password_endpoint(payload: PasswordResetConfirmRequest, db: Session = Depends(get_db)):
    """
        Update a user's password after verifying the reset token.
    """
    reset_password(db, payload)
    return MessageResponse(message="Password updated successfully")












@auth_router.get("/google")
@auth_router.get("/google/login")
def google_login():
    """Redirect the browser to Google's OAuth consent screen."""
    authorization_url, state = build_google_login_url()
    response = RedirectResponse(url=authorization_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        "google_oauth_state",
        state,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/auth/google",
    )
    return response


@auth_router.get("/google/callback", response_model=None)
def google_callback(
    code: str | None = None,
    state: str | None = None,
    oauth_state: str | None = Cookie(default=None, alias="google_oauth_state"),
    db: Session = Depends(get_db),
):
    """Handle Google's callback, create the local session, and redirect to the frontend."""
    if code is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing code parameter")
    if state is None or oauth_state is None or state != oauth_state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state")

    user, is_new_user = continue_with_google(db, code)
    session = issue_token_pair(db, user)

    redirect_response = RedirectResponse(
        url=build_google_frontend_redirect_url(session.access_token, is_new_user),
        status_code=status.HTTP_302_FOUND,
    )
    redirect_response.set_cookie(
        "refresh_token",
        create_refresh_token(user),
        httponly=True,
        secure=False,
        samesite="lax",
        path="/auth",
    )
    redirect_response.delete_cookie("google_oauth_state", path="/auth/google")
    return redirect_response
