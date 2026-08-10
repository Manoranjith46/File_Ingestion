"""Integration tests for the POST /v1/ingest/ftp/init API endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _register_and_login(client) -> tuple[str, str]:
    """Register a test user, verify OTP, login, and return (user_id, bearer_token)."""
    client.post("/auth/signup/init", json={
        "email": "init_test@example.com",
        "username": "inittester",
        "full_name": "Init Tester",
        "password": "StrongP@ss1",
    })
    client.post("/auth/signup/verify", json={
        "email": "init_test@example.com",
        "otp_code": "123456",
    })
    resp = client.post("/auth/login", json={
        "identifier": "inittester",
        "password": "StrongP@ss1",
    })
    data = resp.json()
    token = data.get("access_token", "")
    user_id = data.get("user", {}).get("id", "")
    return user_id, f"Bearer {token}"


class TestFtpInitEndpoint:
    """Integration tests for POST /v1/ingest/ftp/init."""

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_init_success(self, mock_probe, client):
        """Connect session, create dataset, then call /v1/ingest/ftp/init -> 202 Accepted."""
        _, token = _register_and_login(client)

        # 1. Connect FTP
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

        # 2. Create Dataset
        ds_resp = client.post(
            "/v1/datasets",
            json={"name": "FTP Dataset", "description": "Test dataset", "language": "English"},
            headers={"Authorization": token},
        )
        assert ds_resp.status_code == 201
        dataset_id = ds_resp.json()["id"]


        # 3. POST /v1/ingest/ftp/init
        init_resp = client.post(
            "/v1/ingest/ftp/init",
            json={
                "dataset_id": dataset_id,
                "items": [
                    {
                        "path": "/2026_Assets/dataset.csv",
                        "is_folder": False,
                        "size_bytes": 1048576,
                    }
                ],
            },
            headers={"Authorization": token},
        )

        assert init_resp.status_code == 202
        body = init_resp.json()
        assert body["status"] == "processing"
        assert len(body["jobs"]) == 1
        assert body["jobs"][0]["filename"] == "dataset.csv"
        assert body["jobs"][0]["filesize"] == 1048576
        assert body["jobs"][0]["dataset_id"] == dataset_id
        assert body["jobs"][0]["source"] == "FTP"

    def test_init_missing_auth(self, client):
        """POST /v1/ingest/ftp/init without auth header returns 401."""
        resp = client.post(
            "/v1/ingest/ftp/init",
            json={"dataset_id": "ds-1", "items": [{"path": "/a.csv"}]},
        )
        assert resp.status_code == 401

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_init_missing_dataset_returns_404(self, mock_probe, client):
        """POST /v1/ingest/ftp/init with non-existent dataset returns 404."""
        _, token = _register_and_login(client)

        client.post(
            "/v1/ingest/ftp/connect",
            json={"host": "ftp.example.com", "username": "u", "password": "p"},
            headers={"Authorization": token},
        )

        resp = client.post(
            "/v1/ingest/ftp/init",
            json={
                "dataset_id": "non-existent-ds-id",
                "items": [{"path": "/data.csv"}],
            },
            headers={"Authorization": token},
        )

        assert resp.status_code == 404
        assert "Dataset not found" in resp.json()["error"]["message"]

    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_init_on_draining_session_returns_409(self, mock_probe, client):
        """POST /v1/ingest/ftp/init when session is draining returns 409."""
        _, token = _register_and_login(client)

        client.post(
            "/v1/ingest/ftp/connect",
            json={"host": "ftp.example.com", "username": "u", "password": "p"},
            headers={"Authorization": token},
        )

        # Force state to draining via service update
        from services.ext_ftp_service import _update_session_state
        # Extract user_id from token decode or helper
        from services.auth_services import decode_access_token
        user_id = decode_access_token(token.replace("Bearer ", ""))["sub"]
        _update_session_state(user_id, "draining")

        # Create dataset
        ds_resp = client.post(
            "/v1/datasets",
            json={"name": "Draining DS", "language": "English"},
            headers={"Authorization": token},
        )
        assert ds_resp.status_code == 201
        dataset_id = ds_resp.json()["id"]


        resp = client.post(
            "/v1/ingest/ftp/init",
            json={"dataset_id": dataset_id, "items": [{"path": "/data.csv"}]},
            headers={"Authorization": token},
        )

        assert resp.status_code == 409
        assert "draining" in resp.json()["error"]["message"].lower()
