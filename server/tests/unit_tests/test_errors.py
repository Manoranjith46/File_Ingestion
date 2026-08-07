from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret")

from utils.errors import APIErrors, InvalidOTPError, _error  # noqa: E402


def test_dynamic_error_factory_creates_custom_exception() -> None:
    custom_error = _error("CustomExampleError", "custom_example", "Custom example failed", http_status=418, retryable=True)

    exc = custom_error(message="Override message", details={"field": "email"}, cause=ValueError("boom"))

    assert issubclass(custom_error, APIErrors)
    assert exc.code == "custom_example"
    assert exc.public_message == "Override message"
    assert exc.details == {"field": "email"}
    assert exc.http_status == 418
    assert exc.retryable is True


def test_global_exception_handler_returns_expected_payload() -> None:
    app = FastAPI()

    @app.exception_handler(APIErrors)
    async def handler(request, exc: APIErrors):
        return exc.to_response()

    @app.get("/boom")
    async def boom() -> None:
        raise InvalidOTPError(message="Invalid OTP", details={"attempts": 2})

    client = TestClient(app)
    response = client.get("/boom")

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "invalid_otp",
            "message": "Invalid OTP",
            "details": {"attempts": 2},
            "retryable": False,
        }
    }
