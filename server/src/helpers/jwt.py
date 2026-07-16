"""Hashing and token helpers for authentication workflows."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status

from helpers.GetEnv import get_env
from models.auth_model import User


PASSWORD_HASH_ITERATIONS = 120000 #use 600000 in Production Mode
OTP_LENGTH = 6


def _now() -> datetime:
	"""Return the current UTC timestamp."""
	return datetime.now(UTC)


def _access_token_ttl() -> timedelta:
	"""Return the access-token lifetime configured for the application."""
	return timedelta(minutes=int(get_env("ACCESS_TOKEN_EXPIRE_MINUTES", default="15", required=False)))


def _refresh_token_ttl() -> timedelta:
	"""Return the refresh-token lifetime configured for the application."""
	return timedelta(days=int(get_env("REFRESH_TOKEN_EXPIRE_DAYS", default="7", required=False)))


def _auth_secret() -> str:
	"""Return the signing secret for access tokens."""
	return get_env("JWT_SECRET_KEY", default="dev-access-secret", required=False)


def _refresh_secret() -> str:
	"""Return the signing secret for refresh tokens."""
	return get_env("JWT_REFRESH_SECRET_KEY", default=_auth_secret(), required=False)


def _b64url_encode(raw_data: bytes) -> str:
	"""Encode bytes using URL-safe base64 without padding."""
	return base64.urlsafe_b64encode(raw_data).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded_data: str) -> bytes:
	"""Decode URL-safe base64 text with missing padding handled automatically."""
	padding = "=" * (-len(encoded_data) % 4)
	return base64.urlsafe_b64decode(encoded_data + padding)


def _json_dumps(value: dict[str, Any]) -> bytes:
	"""Serialize a JSON object using a stable compact encoding."""
	return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


def _jwt_sign(payload: dict[str, Any], secret: str) -> str:
	"""Sign a JWT payload with HMAC SHA-256."""
	header = {"alg": "HS256", "typ": "JWT"}
	encoded_header = _b64url_encode(_json_dumps(header))
	encoded_payload = _b64url_encode(_json_dumps(payload))
	signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
	signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
	return f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}"


def _jwt_decode(token: str, secret: str, expected_token_type: str) -> dict[str, Any]:
	"""Decode and validate a JWT payload signed with HMAC SHA-256."""
	try:
		encoded_header, encoded_payload, encoded_signature = token.split(".")
		signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
		signature = _b64url_decode(encoded_signature)
		expected_signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()

		if not hmac.compare_digest(signature, expected_signature):
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature")

		payload = json.loads(_b64url_decode(encoded_payload))
		if payload.get("token_type") != expected_token_type:
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

		expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
		if expires_at <= _now():
			raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")

		return payload
	except (ValueError, KeyError, json.JSONDecodeError):
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token format") from None


def hash_password(password: str) -> str:
	"""Hash a password using PBKDF2 with a random salt."""
	salt = secrets.token_bytes(16)
	password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
	return "pbkdf2_sha256${}${}${}".format(
		PASSWORD_HASH_ITERATIONS,
		_b64url_encode(salt),
		_b64url_encode(password_hash),
	)


def verify_password(password: str, password_hash: str) -> bool:
	"""Verify a password against a stored PBKDF2 hash."""
	try:
		algorithm, iterations, encoded_salt, encoded_hash = password_hash.split("$")
		if algorithm != "pbkdf2_sha256":
			return False
		computed_hash = hashlib.pbkdf2_hmac(
			"sha256",
			password.encode("utf-8"),
			_b64url_decode(encoded_salt),
			int(iterations),
		)
		return hmac.compare_digest(_b64url_encode(computed_hash), encoded_hash)
	except ValueError:
		return False


def generate_otp_code() -> str:
	"""Generate a numeric one-time password for verification flows."""
	return "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))


def hash_secret(secret_value: str) -> str:
	"""Hash a short-lived secret using the same password hash format."""
	return hash_password(secret_value)


def verify_secret(secret_value: str, secret_hash: str) -> bool:
	"""Verify a short-lived secret using the password hash checker."""
	return verify_password(secret_value, secret_hash)


def create_access_token(user: User) -> str:
	"""Create a signed access token for the supplied user."""
	expires_at = _now() + _access_token_ttl()
	payload = {
		"sub": user.id,
		"email": user.email,
		"username": user.username,
		"role": user.role,
		"token_type": "access",
		"token_version": user.token_version,
		"iat": int(_now().timestamp()),
		"exp": int(expires_at.timestamp()),
	}
	return _jwt_sign(payload, _auth_secret())


def create_refresh_token(user: User) -> str:
	"""Create a signed refresh token for the supplied user."""
	expires_at = _now() + _refresh_token_ttl()
	payload = {
		"sub": user.id,
		"email": user.email,
		"token_type": "refresh",
		"token_version": user.token_version,
		"iat": int(_now().timestamp()),
		"exp": int(expires_at.timestamp()),
	}
	return _jwt_sign(payload, _refresh_secret())


def decode_access_token(access_token: str) -> dict[str, Any]:
	"""Decode and verify an access token."""
	return _jwt_decode(access_token, _auth_secret(), "access")


def decode_refresh_token(refresh_token: str) -> dict[str, Any]:
	"""Decode and verify a refresh token."""
	return _jwt_decode(refresh_token, _refresh_secret(), "refresh")
