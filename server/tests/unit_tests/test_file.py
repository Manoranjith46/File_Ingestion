"""Unit tests for file service and dataset workflows."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from typing import Generator
from sqlalchemy.orm import Session

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from models.auth_model import Base, User
from models.file_model import Dataset, DatasetFolderFilesMapping, Folder, UploadedFile
from schemas.file_schema import DatasetCreate, UploadInitRequest
from services import file_services


class FakeRedis:
    """Simple Redis stub for file upload session state."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes[key] = {**self.hashes.get(key, {}), **mapping}

    def expire(self, key: str, ttl_seconds: int) -> None:
        return None

    def bitcount(self, key: str) -> int:
        return 0

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.hashes.pop(key, None)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def exists(self, key: str) -> bool:
        return key in self.hashes


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Create an isolated SQLite-backed SQLAlchemy session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point upload storage paths to a temporary directory."""
    upload_root = tmp_path / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(file_services, "UPLOAD_ROOT", upload_root)
    monkeypatch.setattr(file_services, "PARTS_ROOT", upload_root / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", upload_root / "files")
    file_services.PARTS_ROOT.mkdir(parents=True, exist_ok=True)
    file_services.FINAL_ROOT.mkdir(parents=True, exist_ok=True)
    return upload_root


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Patch the file service to use an in-memory Redis stub."""
    redis_stub = FakeRedis()
    monkeypatch.setattr(file_services, "redis_server", redis_stub)
    monkeypatch.setattr(file_services, "atomic_chunk_state", lambda **_: 0)
    return redis_stub


def test_create_dataset_rejects_duplicate_name(db_session: Session) -> None:
    """Duplicate dataset names should produce a conflict response."""
    user = User(email="dataset@example.com", username="dataset", full_name="Dataset User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    payload = DatasetCreate(name="Finance", description="Primary finance data")
    file_services.create_dataset(db_session, user, payload)

    with pytest.raises(HTTPException) as exc_info:
        file_services.create_dataset(db_session, user, payload)

    assert exc_info.value.status_code == 409


def test_initialize_upload_returns_duplicate_short_circuit_for_existing_mapping(db_session: Session, storage_root: Path, fake_redis: FakeRedis) -> None:
    """Existing file mappings should short-circuit the upload flow."""
    user = User(email="upload@example.com", username="upload", full_name="Upload User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Invoices")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    uploaded_file = UploadedFile(filename="invoice.pdf", file_size_bytes=12, master_hash="a" * 64, physical_path=str(storage_root / "invoice.pdf"))
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    payload = UploadInitRequest(dataset_id=dataset.id, filename="invoice.pdf", filesize=12, master_hash="a" * 64)
    response = file_services.initialize_upload(db_session, user, payload)

    assert response.status == "duplicate_short_circuit"
    assert response.total_chunks == 0


def test_initialize_upload_creates_new_session_for_new_upload(db_session: Session, storage_root: Path, fake_redis: FakeRedis) -> None:
    """A fresh upload should create a new upload session with metadata."""
    user = User(email="new-upload@example.com", username="newupload", full_name="New Upload User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Reports")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    payload = UploadInitRequest(dataset_id=dataset.id, filename="report.txt", filesize=1024, master_hash="b" * 64)
    response = file_services.initialize_upload(db_session, user, payload)

    assert response.status == "created"
    assert response.total_chunks == 1
    assert fake_redis.hashes["upload:" + response.upload_id + ":meta"]["dataset_id"] == dataset.id


def test_delete_user_upload_removes_database_row_mappings_and_physical_file(
    db_session: Session,
    storage_root: Path,
) -> None:
    """Deleting a file should remove its database records and stored file."""
    user = User(email="file-owner@example.com", username="fileowner", full_name="File Owner", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    first_dataset = Dataset(user_id=user.id, name="First")
    second_dataset = Dataset(user_id=user.id, name="Second")
    db_session.add_all([first_dataset, second_dataset])
    db_session.commit()
    db_session.refresh(first_dataset)
    db_session.refresh(second_dataset)

    file_path = storage_root / "files" / "shared.txt"
    file_path.write_text("file contents")
    uploaded_file = UploadedFile(
        filename="shared.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="e" * 64,
        physical_path=str(file_path),
    )
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    db_session.add_all([
        DatasetFolderFilesMapping(dataset_id=first_dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id),
        DatasetFolderFilesMapping(dataset_id=second_dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id),
    ])
    db_session.commit()

    response = file_services.delete_user_upload(db_session, user, uploaded_file.id)

    assert response.file_id == uploaded_file.id
    assert not file_path.exists()
    assert db_session.query(UploadedFile).filter(UploadedFile.id == uploaded_file.id).first() is None
    assert db_session.query(DatasetFolderFilesMapping).filter(DatasetFolderFilesMapping.file_id == uploaded_file.id).count() == 0


def test_delete_dataset_conflicts_when_files_are_attached(db_session: Session) -> None:
    """Deleting a dataset should be blocked while active file mappings still exist."""
    user = User(email="dataset-owner@example.com", username="datasetowner", full_name="Dataset Owner", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Protected")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    uploaded_file = UploadedFile(filename="protected.txt", file_size_bytes=64, master_hash="d" * 64, physical_path="/tmp/protected.txt")
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        file_services.delete_dataset(db_session, user, dataset.id)

    assert exc_info.value.status_code == 409


def test_attach_file_to_dataset_requires_user_ownership(db_session: Session) -> None:
    """Attaching a file should be rejected unless the file belongs to the requesting user."""
    owner = User(email="owner@example.com", username="owner", full_name="Owner", password_hash="hash", auth_provider="local", is_verified=True)
    other_user = User(email="other@example.com", username="other", full_name="Other", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add_all([owner, other_user])
    db_session.commit()
    db_session.refresh(owner)
    db_session.refresh(other_user)

    dataset = Dataset(user_id=owner.id, name="Shared")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    uploaded_file = UploadedFile(filename="shared.txt", file_size_bytes=64, master_hash="c" * 64, physical_path="/tmp/shared.txt")
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    with pytest.raises(HTTPException) as exc_info:
        file_services.attach_file_to_dataset(db_session, other_user, dataset.id, SimpleNamespace(file_id=uploaded_file.id, relative_path=None))

    assert exc_info.value.status_code == 403
