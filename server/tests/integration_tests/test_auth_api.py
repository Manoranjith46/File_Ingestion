"""Integration-style tests for authentication endpoints using HTTPX."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from main import app


@pytest.fixture()
def client() -> httpx.AsyncClient:
    """Create an async HTTPX client for the FastAPI app."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_register_and_login_flow(client: httpx.AsyncClient) -> None:
    """Registration and login should succeed for a valid user."""
    unique_suffix = "integration-" + str(__import__("uuid").uuid4()).split("-")[0]
    payload = {
        "email": f"{unique_suffix}@example.com",
        "username": unique_suffix,
        "full_name": "Integration User",
        "password": "password123",
    }
    register_response = await client.post("/auth/signup/init", json=payload)
    assert register_response.status_code == 200

    verify_response = await client.post(
        "/auth/signup/verify",
        json={"email": payload["email"], "otp_code": "123456"},
    )
    assert verify_response.status_code == 200
