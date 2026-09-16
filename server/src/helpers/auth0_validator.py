"""Standard Auth0 RS256 token verification and JWKS key management."""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from fastapi import HTTPException, status

from config.auth0_config import auth0_settings

logger = logging.getLogger(__name__)

# In-memory cache for JWKS keys: {kid: {"key": RSAPublicKey, "expires_at": float}}
_JWKS_CACHE: dict[str, Any] = {}
_JWKS_CACHE_TTL = 3600  # 1 hour cache


def _b64url_decode(encoded_data: str) -> bytes:
    """Decode URL-safe base64 string with auto-padding."""
    padding_needed = "=" * (-len(encoded_data) % 4)
    return base64.urlsafe_b64decode(encoded_data + padding_needed)


def _int_from_b64url(encoded_data: str) -> int:
    """Convert a base64url-encoded string into a big-endian unsigned integer."""
    raw_bytes = _b64url_decode(encoded_data)
    return int.from_bytes(raw_bytes, byteorder="big")


def fetch_jwks(jwks_url: str | None = None) -> dict[str, Any]:
    """Fetch the JSON Web Key Set (JWKS) from Auth0."""
    url = jwks_url or auth0_settings.jwks_url
    if not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth0 JWKS URL is not configured",
        )
    try:
        response = httpx.get(url, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except Exception as err:
        logger.error(f"Failed to fetch JWKS from {url}: {err}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Unable to verify token signature: JWKS retrieval failed ({err})",
        ) from err


def get_public_key_for_kid(kid: str, force_refresh: bool = False):
    """Retrieve and construct the RSA public key for a given kid from JWKS."""
    now = time.time()
    if not force_refresh and kid in _JWKS_CACHE:
        cached = _JWKS_CACHE[kid]
        if cached["expires_at"] > now:
            return cached["key"]

    jwks = fetch_jwks()
    keys = jwks.get("keys", [])
    found_key = None

    for key_data in keys:
        k_id = key_data.get("kid")
        if key_data.get("kty") == "RSA" and "n" in key_data and "e" in key_data:
            n = _int_from_b64url(key_data["n"])
            e = _int_from_b64url(key_data["e"])
            public_key = RSAPublicNumbers(e, n).public_key()
            _JWKS_CACHE[k_id] = {
                "key": public_key,
                "expires_at": now + _JWKS_CACHE_TTL,
            }
            if k_id == kid:
                found_key = public_key

    if found_key is not None:
        return found_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Token signed by unknown key ID (kid: {kid})",
    )


def verify_auth0_token(
    token: str,
    expected_audience: str | None = None,
    expected_issuer: str | None = None,
) -> dict[str, Any]:
    """Validate an Auth0 RS256-signed JWT access token against JWKS.

    Verifies:
        - Format (header.payload.signature)
        - Algorithm is RS256
        - Signature valid against Auth0 JWKS public key
        - Expiration time (exp)
        - Issuer (iss)
        - Audience (aud)

    Returns:
        dict[str, Any]: Verified claims dictionary including sub, org_id, email, etc.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token format",
        )

    encoded_header, encoded_payload, encoded_signature = parts

    try:
        header = json.loads(_b64url_decode(encoded_header))
        payload = json.loads(_b64url_decode(encoded_payload))
        signature = _b64url_decode(encoded_signature)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token header or payload",
        )

    alg = header.get("alg")
    if alg != "RS256":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unsupported algorithm '{alg}'. Only RS256 is supported",
        )

    kid = header.get("kid")
    if not kid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token header missing key ID (kid)",
        )

    public_key = get_public_key_for_kid(kid)

    # Verify signature
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    try:
        public_key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception:
        # Retry with force refresh once in case key was rotated
        try:
            refreshed_key = get_public_key_for_kid(kid, force_refresh=True)
            refreshed_key.verify(
                signature,
                signing_input,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token signature",
            )

    # Verify expiration
    exp = payload.get("exp")
    if exp is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing expiration claim (exp)",
        )
    expires_at = datetime.fromtimestamp(int(exp), tz=UTC)
    if expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )

    # Verify Issuer
    issuer = expected_issuer or auth0_settings.issuer
    token_iss = payload.get("iss")
    if issuer:
        # Standardize trailing slash comparison
        if not token_iss or token_iss.rstrip("/") != issuer.rstrip("/"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token issuer: expected {issuer}, got {token_iss}",
            )

    # Verify Audience
    audience = expected_audience or auth0_settings.audience
    token_aud = payload.get("aud")
    if audience:
        if isinstance(token_aud, list):
            if audience not in token_aud:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid token audience: {audience} not in {token_aud}",
                )
        elif token_aud != audience:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token audience: expected {audience}, got {token_aud}",
            )

    # Verify Subject
    if not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject claim (sub)",
        )

    return payload
