"""Integration tests for the GET /v1/ingest/ftp/tree API endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _register_and_login(client) -> tuple[str, str]:
    """Register a test user, verify OTP, login, and return (user_id, bearer_token)."""
    client.post("/auth/signup/init", json={
        "email": "tree_test@example.com",
        "username": "treetester",
        "full_name": "Tree Tester",
        "password": "StrongP@ss1",
    })
    client.post("/auth/signup/verify", json={
        "email": "tree_test@example.com",
        "otp_code": "123456",
    })
    resp = client.post("/auth/login", json={
        "identifier": "treetester",
        "password": "StrongP@ss1",
    })
    data = resp.json()
    token = data.get("access_token", "")
    user_id = data.get("user", {}).get("id", "")
    return user_id, f"Bearer {token}"


class TestFtpTreeEndpoint:
    """Integration tests for GET /v1/ingest/ftp/tree."""

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_get_tree_success(self, mock_probe, mock_ftp_tls_cls, client):
        """Connect first, then fetch /v1/ingest/ftp/tree -> 200 OK with parsed items."""
        _, token = _register_and_login(client)

        # 1. Connect
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

        # 2. Mock MLSD response
        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp
        mock_ftp.mlsd.return_value = [
            ("dataset.csv", {"type": "file", "size": "1048576", "modify": "20260801120000"}),
            ("logs", {"type": "dir", "modify": "20260802103000"}),
        ]

        # 3. GET tree
        resp = client.get(
            "/v1/ingest/ftp/tree?path=/2026_Assets",
            headers={"Authorization": token},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["current_path"] == "/2026_Assets"
        assert body["parent_path"] == "/"
        assert len(body["items"]) == 2

        # Verify folder sorting
        assert body["items"][0]["name"] == "logs"
        assert body["items"][0]["is_folder"] is True
        assert body["items"][1]["name"] == "dataset.csv"
        assert body["items"][1]["is_folder"] is False

    def test_get_tree_missing_auth(self, client):
        """GET /v1/ingest/ftp/tree without Authorization header returns 401."""
        resp = client.get("/v1/ingest/ftp/tree")
        assert resp.status_code == 401

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    def test_get_tree_without_session_returns_404(self, mock_ftp_tls_cls, client):
        """GET /v1/ingest/ftp/tree without connecting first returns 404."""
        _, token = _register_and_login(client)
        resp = client.get(
            "/v1/ingest/ftp/tree?path=/",
            headers={"Authorization": token},
        )
        assert resp.status_code == 404
        assert "No active external FTP session" in resp.json()["error"]["message"]

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_get_tree_nonexistent_remote_path_returns_404(self, mock_probe, mock_ftp_tls_cls, client):
        import ftplib

        _, token = _register_and_login(client)

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

        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp
        mock_ftp.mlsd.side_effect = ftplib.error_perm("550 Directory not found")

        resp = client.get(
            "/v1/ingest/ftp/tree?path=/nonexistent_dir",
            headers={"Authorization": token},
        )
        assert resp.status_code == 404
        assert "does not exist" in resp.json()["error"]["message"]

