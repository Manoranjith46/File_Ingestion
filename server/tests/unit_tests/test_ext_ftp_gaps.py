"""Unit tests for External FTP Gap Implementations (Updates.md L41-L493)

Covers:
  - Filename collision 409 vs auto_rename=true in initiate_external_ftp_ingestion
  - Batch UI polling get_batch_ingestion_status reading live Redis progress bytes
  - Resume failed jobs success vs session expired (410 Gone)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-ftp-gaps")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-ftp-gaps")
os.environ.setdefault("ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

from models.auth_model import Base as AuthBase, User
from models.file_model import (
    Base as FileBase,
    Dataset,
    IngestedFile,
    IngestedFileProviderType,
)
from services import ext_ftp_service
from services.ext_ftp_service import (
    get_batch_ingestion_status,
    initiate_external_ftp_ingestion,
    resume_failed_ftp_jobs,
)
from utils.errors import (
    FilenameCollisionError,
    FtpSessionExpiredError,
    IngestionJobNotFoundError,
)


class FakeRedis:
    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self._data:
            return False
        self._data[key] = str(value)
        return True

    def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._data:
                del self._data[k]
                count += 1
        return count


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    AuthBase.metadata.create_all(bind=engine)
    FileBase.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        AuthBase.metadata.drop_all(bind=engine)
        FileBase.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        id="user-ftp-gaps",
        email="ftp_gaps@example.com",
        username="ftp_gaps_user",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def dataset(db_session: Session, user: User) -> Dataset:
    d = Dataset(id="ds-ftp-gaps", user_id=user.id, name="FTP Gaps DS", language="English")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


def _seed_active_session(fake_redis: FakeRedis, user_id: str):
    import json
    from helpers.fernet_vault import fernet_encrypt

    payload = {
        "host": "ftp.example.com",
        "port": 21,
        "username": "user",
        "encrypted_password": fernet_encrypt("secret"),
        "protocol": "ftps",
        "state": "active",
    }
    fake_redis.set(f"ext_ftp:creds:{user_id}", json.dumps(payload))


# ===========================================================================
# GAP 1: Virtual Deduplication & auto_rename in FTP Init
# ===========================================================================


def test_ftp_init_filename_collision_409(
    db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis
):
    """Queueing a file that collides with existing dataset file raises 409 when auto_rename=False."""
    _seed_active_session(fake_redis, user.id)

    # Seed existing file in dataset
    uf = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.FTP,
        file_path="existing.csv",
        filename="existing.csv",
        file_size_bytes=1024,
        master_hash="a" * 64,
        status="completed",
    )
    db_session.add(uf)
    db_session.commit()

    with patch.object(ext_ftp_service, "redis_server", fake_redis):
        with pytest.raises(FilenameCollisionError) as exc_info:
            initiate_external_ftp_ingestion(
                user=user,
                db=db_session,
                dataset_id=dataset.id,
                target_folder_id=None,
                items=[{"path": "/remote/existing.csv", "is_folder": False, "size_bytes": 1024}],
                auto_rename=False,
            )

        assert exc_info.value.http_status == 409
        assert exc_info.value.details["suggestion"] == "existing (1).csv"


def test_ftp_init_filename_collision_auto_rename(
    db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis
):
    """Queueing a file with collision when auto_rename=True renames job to existing (1).csv."""
    _seed_active_session(fake_redis, user.id)

    uf = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.FTP,
        file_path="existing.csv",
        filename="existing.csv",
        file_size_bytes=1024,
        master_hash="a" * 64,
        status="completed",
    )
    db_session.add(uf)
    db_session.commit()

    with patch.object(ext_ftp_service, "redis_server", fake_redis):
        res = initiate_external_ftp_ingestion(
            user=user,
            db=db_session,
            dataset_id=dataset.id,
            target_folder_id=None,
            items=[{"path": "/remote/existing.csv", "is_folder": False, "size_bytes": 1024}],
            auto_rename=True,
        )

        assert res["status"] == "processing"
        assert res["jobs"][0]["filename"] == "existing (1).csv"


# ===========================================================================
# GAP 2: Batch UI Polling Endpoint Logic
# ===========================================================================


def test_get_batch_ingestion_status_reads_redis_progress(
    db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis
):
    """get_batch_ingestion_status returns jobs and reads live Redis bytes for in_progress jobs."""
    job1 = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.FTP,
        file_path="/remote/file1.bin",
        filename="file1.bin",
        status="in_progress",
        file_size_bytes=1000000,
    )
    job2 = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.FTP,
        file_path="/remote/file2.bin",
        filename="file2.bin",
        status="completed",
        file_size_bytes=500000,
    )
    db_session.add_all([job1, job2])
    db_session.commit()

    # Set live progress in Redis for job1
    fake_redis.set(f"ingest:{job1.id}:progress", "500000")

    with patch.object(ext_ftp_service, "redis_server", fake_redis):
        status_res = get_batch_ingestion_status(user=user, db=db_session, status_filter="in_progress,completed")

        assert status_res["total_active_jobs"] == 2
        jobs_map = {j["job_id"]: j for j in status_res["jobs"]}

        # job1 live progress read from Redis
        assert jobs_map[job1.id]["bytes_downloaded"] == 500000
        assert jobs_map[job1.id]["progress_percentage"] == 50
        assert jobs_map[job2.id]["status"] == "completed"


# ===========================================================================
# GAP 4: Medic Recovery (POST /v1/ingest/ftp/resume)
# ===========================================================================


def test_resume_failed_ftp_jobs_success(
    db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis
):
    """Resuming failed jobs resets status to pending and queues background workers."""
    _seed_active_session(fake_redis, user.id)

    job = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.FTP,
        file_path="/remote/failed.bin",
        filename="failed.bin",
        status="failed",
        error_message="Connection lost",
    )
    db_session.add(job)
    db_session.commit()

    with patch.object(ext_ftp_service, "redis_server", fake_redis), \
         patch.object(ext_ftp_service, "process_ftp_ingestion_job", MagicMock()):

        res = resume_failed_ftp_jobs(user=user, db=db_session, job_ids=[job.id])

        assert res["status"] == "processing"
        assert job.id in res["resumed_jobs"]

        db_session.refresh(job)
        assert job.status == "pending"
        assert job.error_message is None


def test_resume_failed_ftp_jobs_session_expired_410(
    db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis
):
    """Resuming failed jobs when Redis Vault session expired raises FtpSessionExpiredError (410 Gone)."""
    job = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.FTP,
        file_path="/remote/failed.bin",
        filename="failed.bin",
        status="failed",
    )
    db_session.add(job)
    db_session.commit()

    with patch.object(ext_ftp_service, "redis_server", fake_redis):
        with pytest.raises(FtpSessionExpiredError) as exc_info:
            resume_failed_ftp_jobs(user=user, db=db_session, job_ids=[job.id])

        assert exc_info.value.http_status == 410
