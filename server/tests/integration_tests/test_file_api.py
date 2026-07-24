"""Integration-style tests for file and dataset endpoints using HTTPX."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from src.main import app


@pytest.fixture()
def client() -> httpx.AsyncClient:
    """Create an async HTTPX client for file endpoints."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_dataset_creation_requires_auth_header(client: httpx.AsyncClient) -> None:
    """Dataset creation should reject requests without an authorization header."""
    response = await client.post("/v1/datasets", json={"name": "Remote"})
    assert response.status_code == 401
