"""Pydantic schemas for auth requests, responses, and session payloads."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator, model_validator
from email_validator import validate_email, EmailNotValidError
from helpers.get_env import get_env


class PublicUserSchema(BaseModel):
    """
        Represent the public shape of a user returned by auth endpoints.
        It also used to Control the data returned by the /me endpoint, ensuring that sensitive information like password hashes are not exposed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    username: str | None = None
    full_name: str | None = None
    role: str
    is_verified: bool
    auth_provider: str


class RegisterRequest(BaseModel):
    """Validate a user registration request."""

    email: EmailStr = Field(min_length=5, max_length=255)
    username: str | None = Field(default=None, min_length=3, max_length=64)
    full_name: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email", mode="before")
    def _strip_email(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("email", mode="after")
    def _validate_email_format(cls, v):
        # v is already validated as EmailStr at this point; run stricter checks and normalization
        try:
            check_deliver = get_env("EMAIL_CHECK_DELIVERABILITY", default="false", required=False).lower() in ("1", "true", "yes")
        except Exception:
            check_deliver = False
        try:
            info = validate_email(str(v), check_deliverability=check_deliver)
            return info.email
        except EmailNotValidError as e:
            raise ValueError(str(e))

    @field_validator("username", mode="before")
    def _strip_username(cls, v):
        if isinstance(v, str):
            v2 = v.strip()
            if v2 == "":
                raise ValueError("username cannot be blank or only whitespace")
            return v2
        return v

    @field_validator("full_name", mode="before")
    def _strip_full_name(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class LoginRequest(BaseModel):
    """Validate a username/email plus password login request."""

    identifier: str | None = Field(default=None, min_length=3, max_length=255)
    username: str | None = Field(default=None, min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="before")
    def _normalize_identifier(cls, values):
        if isinstance(values, dict):
            normalized = dict(values)
            if not normalized.get("identifier") and normalized.get("username"):
                normalized["identifier"] = normalized["username"]
            return normalized
        return values

    @field_validator("identifier", mode="before")
    def _strip_identifier(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class OtpRequest(BaseModel):
    """Validate a request to generate a fresh OTP for a user."""

    email: EmailStr = Field(min_length=5, max_length=255)

    @field_validator("email", mode="before")
    def _strip_email(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("email", mode="after")
    def _validate_email_format(cls, v):
        try:
            check_deliver = get_env("EMAIL_CHECK_DELIVERABILITY", default="false", required=False).lower() in ("1", "true", "yes")
        except Exception:
            check_deliver = False
        try:
            info = validate_email(str(v), check_deliverability=check_deliver)
            return info.email
        except EmailNotValidError as e:
            raise ValueError(str(e))


class OtpVerifyRequest(BaseModel):
    """Validate an OTP verification submission."""

    email: EmailStr = Field(min_length=5, max_length=255)
    otp_code: str = Field(min_length=4, max_length=12)

    @field_validator("email", mode="before")
    def _strip_email(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("email", mode="after")
    def _validate_email_format(cls, v):
        try:
            check_deliver = get_env("EMAIL_CHECK_DELIVERABILITY", default="false", required=False).lower() in ("1", "true", "yes")
        except Exception:
            check_deliver = False
        try:
            info = validate_email(str(v), check_deliverability=check_deliver)
            return info.email
        except EmailNotValidError as e:
            raise ValueError(str(e))


class PasswordResetRequest(BaseModel):
    """Validate a password-reset request identifier."""

    email: EmailStr = Field(min_length=5, max_length=255)

    @field_validator("email", mode="before")
    def _strip_email(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("email", mode="after")
    def _validate_email_format(cls, v):
        try:
            check_deliver = get_env("EMAIL_CHECK_DELIVERABILITY", default="false", required=False).lower() in ("1", "true", "yes")
        except Exception:
            check_deliver = False
        try:
            info = validate_email(str(v), check_deliverability=check_deliver)
            return info.email
        except EmailNotValidError as e:
            raise ValueError(str(e))


class PasswordResetConfirmRequest(BaseModel):
    """Validate a password reset token and new password."""

    email: EmailStr = Field(min_length=5, max_length=255)
    reset_token: str = Field(min_length=16, max_length=255)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("email", mode="before")
    def _strip_email(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v


class GoogleSignInRequest(BaseModel):
    """Validate a Google identity token exchange request."""

    id_token: str = Field(min_length=20)


class TokenPairResponse(BaseModel):
    """Return a safe authentication success payload to the client."""

    message: str = "Authentication successful"
    user: PublicUserSchema


class RegistrationResponse(BaseModel):
    """Return registration details without exposing OTP secrets."""

    user: PublicUserSchema
    message: str = "Registration successful"


class OtpChallengeResponse(BaseModel):
    """Return a safe OTP acknowledgment."""

    email: str
    message: str = "OTP created"


class PasswordResetChallengeResponse(BaseModel):
    """Return a safe password-reset acknowledgment."""

    email: str
    message: str = "Password reset created"


class MessageResponse(BaseModel):
    """Return a simple message payload."""

    message: str