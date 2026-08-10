"""Integration tests for External FTP Gap HTTP Endpoints (Updates.md L41-L493)

Covers:
  - POST /v1/ingest/ftp/init filename collision 409 vs auto_rename 202
  - GET /v1/ingest/status batch progress polling
  - POST /v1/ingest/ftp/resume 410 Gone vs 202 Accepted
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from models.auth_model import User
from models.file_model import AsyncIngestionJob, Dataset, DatasetFolderFilesMapping, UploadedFile
from services import ext_ftp_service


def _get_auth_headers(client: TestClient) -> dict[str, str]:
    client.post(
        "/v1/auth/signup/init",
        json={"email": "ftp_gaps_api@example.com", "username": "ftp_gaps_api", "password": "Password123!"},
    )
    client.post(
        "/v1/auth/signup/verify",
        json={"email": "ftp_gaps_api@example.com", "otp_code": "123456"},
    )
    login_res = client.post(
        "/v1/auth/login",
        json={"identifier": "ftp_gaps_api@example.com", "password": "Password123!"},
    )
    token = login_res.json().get("access_token") or login_res.json().get("data", {}).get("access_token", "")
    return {"Authorization": f"Bearer {token}"}


@patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
def test_ftp_init_collision_409_api(mock_probe, client: TestClient):
    headers = _get_auth_headers(client)

    # Connect session
    client.post(
        "/v1/ingest/ftp/connect",
        json={"host": "ftp.example.com", "port": 21, "username": "user", "password": "password"},
        headers=headers,
    )

    ds_res = client.post("/v1/datasets", json={"name": "FTP API DS", "language": "English"}, headers=headers)
    dataset_id = ds_res.json()["id"]

    from routes.file_routes import get_db
    from main import app
    db: Session = next(app.dependency_overrides[get_db]())
    user = db.query(User).filter(User.email == "ftp_gaps_api@example.com").first()

    uf = UploadedFile(filename="data_export.csv", file_size_bytes=2048, master_hash="b" * 64, physical_path="/dev/null")
    db.add(uf)
    db.flush()

    mapping = DatasetFolderFilesMapping(dataset_id=dataset_id, folder_id=None, file_id=uf.id, user_id=user.id)
    db.add(mapping)
    db.commit()

    init_res = client.post(
        "/v1/ingest/ftp/init",
        json={
            "dataset_id": dataset_id,
            "auto_rename": False,
            "items": [{"path": "/remote/data_export.csv", "is_folder": False, "size_bytes": 2048}],
        },
        headers=headers,
    )

    assert init_res.status_code == 409
    err_body = init_res.json()
    assert err_body["error"]["code"] == "filename_collision"
    assert err_body["error"]["details"]["suggestion"] == "data_export (1).csv"


@patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
def test_ftp_init_collision_auto_rename_api(mock_probe, client: TestClient):
    headers = _get_auth_headers(client)

    client.post(
        "/v1/ingest/ftp/connect",
        json={"host": "ftp.example.com", "port": 21, "username": "user", "password": "password"},
        headers=headers,
    )

    ds_res = client.post("/v1/datasets", json={"name": "FTP API DS 2", "language": "English"}, headers=headers)
    dataset_id = ds_res.json()["id"]

    from routes.file_routes import get_db
    from main import app
    db: Session = next(app.dependency_overrides[get_db]())
    user = db.query(User).filter(User.email == "ftp_gaps_api@example.com").first()

    uf = UploadedFile(filename="report.pdf", file_size_bytes=1024, master_hash="c" * 64, physical_path="/dev/null")
    db.add(uf)
    db.flush()

    mapping = DatasetFolderFilesMapping(dataset_id=dataset_id, folder_id=None, file_id=uf.id, user_id=user.id)
    db.add(mapping)
    db.commit()

    init_res = client.post(
        "/v1/ingest/ftp/init",
        json={
            "dataset_id": dataset_id,
            "auto_rename": True,
            "items": [{"path": "/remote/report.pdf", "is_folder": False, "size_bytes": 1024}],
        },
        headers=headers,
    )

    assert init_res.status_code == 202
    res_json = init_res.json()
    assert res_json["status"] == "processing"
    assert res_json["jobs"][0]["filename"] == "report (1).pdf"


def test_get_batch_ingestion_status_api(client: TestClient):
    headers = _get_auth_headers(client)

    status_res = client.get("/v1/ingest/status", headers=headers)
    assert status_res.status_code == 200
    res_json = status_res.json()
    assert "total_active_jobs" in res_json
    assert isinstance(res_json["jobs"], list)


def test_ftp_resume_session_expired_410_api(client: TestClient):
    headers = _get_auth_headers(client)

    resume_res = client.post(
        "/v1/ingest/ftp/resume",
        json={"job_ids": ["some-job-id"]},
        headers=headers,
    )

    assert resume_res.status_code == 410
    err_body = resume_res.json()
    assert err_body["error"]["code"] == "ftp_session_expired"


@patch("services.ext_ftp_service._probe_ftp_connection", return_value="ftps")
def test_ftp_resume_success_202_api(mock_probe, client: TestClient):
    headers = _get_auth_headers(client)

    client.post(
        "/v1/ingest/ftp/connect",
        json={"host": "ftp.example.com", "port": 21, "username": "user", "password": "password"},
        headers=headers,
    )

    from routes.file_routes import get_db
    from main import app
    db: Session = next(app.dependency_overrides[get_db]())
    user = db.query(User).filter(User.email == "ftp_gaps_api@example.com").first()

    ds = Dataset(id="ds-resume-test", user_id=user.id, name="Resume DS", language="English")
    db.add(ds)
    db.commit()

    failed_job = AsyncIngestionJob(
        user_id=user.id,
        dataset_id=ds.id,
        provider="FTP",
        source_url_or_id="/remote/failed_api.csv",
        filename="failed_api.csv",
        status="failed",
        error_message="Stream severed",
    )
    db.add(failed_job)
    db.commit()

    with patch.object(ext_ftp_service, "process_ftp_ingestion_job", MagicMock()):
        resume_res = client.post(
            "/v1/ingest/ftp/resume",
            json={"job_ids": [failed_job.id]},
            headers=headers,
        )

        assert resume_res.status_code == 202
        res_json = resume_res.json()
        assert res_json["status"] == "processing"
        assert failed_job.id in res_json["resumed_jobs"]
