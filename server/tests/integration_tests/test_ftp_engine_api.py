"""Integration tests for Component 4 background worker engine and recovery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _register_and_login(client) -> tuple[str, str]:
    """Register a test user, verify OTP, login, and return (user_id, bearer_token)."""
    client.post("/auth/signup/init", json={
        "email": "engine_test@example.com",
        "username": "enginetester",
        "full_name": "Engine Tester",
        "password": "StrongP@ss1",
    })
    client.post("/auth/signup/verify", json={
        "email": "engine_test@example.com",
        "otp_code": "123456",
    })
    resp = client.post("/auth/login", json={
        "identifier": "enginetester",
        "password": "StrongP@ss1",
    })
    data = resp.json()
    token = data.get("access_token", "")
    user_id = data.get("user", {}).get("id", "")
    return user_id, f"Bearer {token}"


class TestFtpEngineIntegration:
    """Integration tests for process_ftp_ingestion_job end-to-end processing."""

    @patch("services.ext_ftp_service.ftplib.FTP_TLS")
    @patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
    def test_full_ingestion_round_trip(self, mock_probe, mock_ftp_tls_cls, client):
        """End-to-end test: Connect -> Create Dataset -> Init Job -> Process Job -> Completed in DB."""
        from config.database import get_session_local
        db_session = get_session_local()()

        _, token = _register_and_login(client)

        # 1. Connect FTP
        client.post(
            "/v1/ingest/ftp/connect",
            json={"host": "ftp.example.com", "port": 21, "username": "u", "password": "p"},
            headers={"Authorization": token},
        )

        # 2. Create Dataset
        ds_resp = client.post(
            "/v1/datasets",
            json={"name": "Engine Test DS", "language": "English"},
            headers={"Authorization": token},
        )
        dataset_id = ds_resp.json()["id"]

        # 3. Queue Job via /init
        init_resp = client.post(
            "/v1/ingest/ftp/init",
            json={
                "dataset_id": dataset_id,
                "items": [{"path": "/2026_Assets/sample.csv", "is_folder": False, "size_bytes": 100}],
            },
            headers={"Authorization": token},
        )
        job_id = init_resp.json()["jobs"][0]["job_id"]

        # 4. Mock FTP RETR callback
        mock_ftp = MagicMock()
        mock_ftp_tls_cls.return_value = mock_ftp

        def mock_retr(cmd, callback, blocksize=8192):
            callback(b"col1,col2\nval1,val2\n")

        mock_ftp.retrbinary.side_effect = mock_retr

        # 5. Process job via service worker
        from services.ext_ftp_service import process_ftp_ingestion_job
        success = process_ftp_ingestion_job(db_session, job_id)
        assert success is True

        # 6. Verify job status in DB
        from models.file_model import AsyncIngestionJob
        job = db_session.query(AsyncIngestionJob).filter(AsyncIngestionJob.id == job_id).first()
        assert job is not None
        assert job.status == "completed"
        assert job.progress_percentage == 100
        assert job.master_hash is not None
        assert job.file_id is not None
