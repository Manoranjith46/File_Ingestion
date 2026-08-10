"""Integration tests for the External FTP Pull connect/disconnect API endpoints.

Uses the shared ``client`` fixture from ``conftest.py`` which provides an
in-memory SQLite database and mocked Redis.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper — create a test user and get a valid bearer token
# ---------------------------------------------------------------------------


def _register_and_login(client) -> tuple[str, str]:
    """Register a user, verify OTP, login, and return (user_id, bearer_token).

    Uses the existing auth endpoints which are already tested.
    """
    # Signup
    client.post("/auth/signup/init", json={
        "email": "ftptest@example.com",
        "username": "ftptester",
        "full_name": "FTP Tester",
        "password": "StrongP@ss1",
    })
    # Verify (default OTP is 123456)
    client.post("/auth/signup/verify", json={
        "email": "ftptest@example.com",
        "otp_code": "123456",
    })
    # Login
    resp = client.post("/auth/login", json={
        "identifier": "ftptester",
        "password": "StrongP@ss1",
    })
    data = resp.json()
    token = data.get("access_token", "")
    user_id = data.get("user", {}).get("id", "")
    return user_id, f"Bearer {token}"


# ---------------------------------------------------------------------------
# POST /v1/ingest/ftp/connect
# ---------------------------------------------------------------------------


class TestFtpConnectEndpoint:
    """Integration tests for POST /v1/ingest/ftp/connect."""

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_connect_success(self, mock_probe, client):
        """A valid connect request returns 200 with session metadata."""
        _, token = _register_and_login(client)

        resp = client.post(
            "/v1/ingest/ftp/connect",
            json={
                "host": "ftp.example.com",
                "port": 21,
                "username": "ftp_user",
                "password": "secure_password",
                "duration_seconds": 7200,
                "until_i_stop": False,
                "graceful_expiry": True,
                "allow_insecure": False,
            },
            headers={"Authorization": token},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["session"]["host"] == "ftp.example.com"
        assert body["session"]["protocol"] == "ftps"
        assert body["session"]["state"] == "active"
        assert body["session"]["idle_ttl_seconds"] == 1800

    def test_connect_missing_auth(self, client):
        """A connect request without auth returns 401."""
        resp = client.post(
            "/v1/ingest/ftp/connect",
            json={
                "host": "ftp.example.com",
                "port": 21,
                "username": "u",
                "password": "p",
            },
        )
        assert resp.status_code == 401

    def test_connect_invalid_body(self, client):
        """A connect request with empty host returns 422."""
        _, token = _register_and_login(client)
        resp = client.post(
            "/v1/ingest/ftp/connect",
            json={
                "host": "",
                "username": "u",
                "password": "p",
            },
            headers={"Authorization": token},
        )
        assert resp.status_code == 422

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_connect_rate_limit_enforced(self, mock_probe, client):
        """After 5 connects, the 6th returns 429."""
        _, token = _register_and_login(client)

        for i in range(5):
            resp = client.post(
                "/v1/ingest/ftp/connect",
                json={
                    "host": f"ftp{i}.example.com",
                    "port": 21,
                    "username": "u",
                    "password": "p",
                },
                headers={"Authorization": token},
            )
            assert resp.status_code == 200, f"Request {i+1} failed unexpectedly"

        resp = client.post(
            "/v1/ingest/ftp/connect",
            json={
                "host": "ftp6.example.com",
                "port": 21,
                "username": "u",
                "password": "p",
            },
            headers={"Authorization": token},
        )
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# POST /v1/ingest/ftp/disconnect
# ---------------------------------------------------------------------------


class TestFtpDisconnectEndpoint:
    """Integration tests for POST /v1/ingest/ftp/disconnect."""

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_disconnect_no_session_returns_404(self, mock_probe, client):
        """Disconnecting without a session returns 404."""
        _, token = _register_and_login(client)

        resp = client.post(
            "/v1/ingest/ftp/disconnect",
            headers={"Authorization": token},
        )
        assert resp.status_code == 404

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_disconnect_after_connect_success(self, mock_probe, client):
        """Connect then disconnect with no active jobs returns 200."""
        _, token = _register_and_login(client)

        # Connect first
        client.post(
            "/v1/ingest/ftp/connect",
            json={
                "host": "ftp.example.com",
                "port": 21,
                "username": "u",
                "password": "p",
            },
            headers={"Authorization": token},
        )

        # Disconnect (no active FTP jobs in DB)
        resp = client.post(
            "/v1/ingest/ftp/disconnect",
            headers={"Authorization": token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"

    def test_disconnect_missing_auth(self, client):
        """Disconnect without auth returns 401."""
        resp = client.post("/v1/ingest/ftp/disconnect")
        assert resp.status_code == 401
