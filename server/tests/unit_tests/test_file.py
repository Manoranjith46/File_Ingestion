"""Unit tests for file service and dataset workflows."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from typing import Generator
from sqlalchemy.orm import Session

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from models.auth_model import Base, User
from models.file_model import Dataset, IngestedFile, IngestedFileProviderType
from schemas.file_schema import DatasetCreate, DatasetUpdate, UploadInitRequest
from services import file_services
from services import gdrive_service
from services import sharepoint_service


class FakeRedis:
    """Simple Redis stub for file upload session state."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.bitmaps: dict[str, set[int]] = {}
        self.kv: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.kv.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.kv:
            return False
        self.kv[key] = str(value)
        return True

    def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes[key] = {**self.hashes.get(key, {}), **mapping}

    def expire(self, key: str, ttl_seconds: int) -> None:
        return None

    def setbit(self, key: str, offset: int, value: int) -> None:
        if key not in self.bitmaps:
            self.bitmaps[key] = set()
        if value:
            self.bitmaps[key].add(offset)
        else:
            self.bitmaps[key].discard(offset)

    def bitcount(self, key: str) -> int:
        return len(self.bitmaps.get(key, set()))

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.hashes.pop(key, None)
            self.bitmaps.pop(key, None)
            self.kv.pop(key, None)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def exists(self, key: str) -> bool:
        return key in self.hashes or key in self.bitmaps or key in self.kv


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
    def _stub_atomic(keys, args):
        bitmap_key = keys[0]
        chunk_idx = int(args[0])
        redis_stub.setbit(bitmap_key, chunk_idx, 1)
        return 0
    monkeypatch.setattr(file_services, "redis_server", redis_stub)
    monkeypatch.setattr(file_services, "atomic_chunk_state", _stub_atomic)
    return redis_stub


def test_create_dataset_rejects_duplicate_name(db_session: Session) -> None:
    """Duplicate dataset names should produce a conflict response."""
    user = User(email="dataset@example.com", username="dataset", full_name="Dataset User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    payload = DatasetCreate(name="Finance", description="Primary finance data", language="English")
    file_services.create_dataset(db_session, user, payload)

    with pytest.raises(HTTPException) as exc_info:
        file_services.create_dataset(db_session, user, payload)

    assert exc_info.value.status_code == 409


def test_dataset_status_is_persisted_and_returned_on_update(db_session: Session) -> None:
    """Dataset status should be stored in the database and exposed by the update flow."""
    user = User(email="status@example.com", username="status", full_name="Status User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    created = file_services.create_dataset(db_session, user, DatasetCreate(name="Status Dataset", language="English"))
    assert created.status == "Created"

    completed = file_services.update_dataset(db_session, user, created.id, DatasetUpdate(status="Completed"))
    assert completed.status == "Completed"


def test_attach_file_to_dataset_multi_file_transitions(db_session: Session) -> None:
    """Attaching multiple files should transition dataset to Draft."""
    user = User(email="multi@example.com", username="multi", full_name="Multi User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Multi Dataset", language="English"))
    first_file = IngestedFile(user_id=user.id, provider=IngestedFileProviderType.Local, file_path="a.txt", filename="a.txt", file_size_bytes=1, master_hash="1" * 64, physical_path="/tmp/a.txt", status="completed")
    second_file = IngestedFile(user_id=user.id, provider=IngestedFileProviderType.Local, file_path="b.txt", filename="b.txt", file_size_bytes=1, master_hash="2" * 64, physical_path="/tmp/b.txt", status="completed")
    db_session.add_all([first_file, second_file])
    db_session.commit()

    file_services.attach_file_to_dataset(db_session, user, dataset.id, SimpleNamespace(file_id=first_file.id, relative_path=None))
    db_session.refresh(dataset)
    assert dataset.status == "Draft"

    file_services.attach_file_to_dataset(db_session, user, dataset.id, SimpleNamespace(file_id=second_file.id, relative_path=None))
    db_session.refresh(dataset)
    assert dataset.status == "Draft"


def test_update_dataset_can_move_mappings_to_another_dataset(db_session: Session) -> None:
    """Dataset updates should allow moving files to another eligible dataset."""
    user = User(email="move@example.com", username="move", full_name="Move User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    source_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Source Dataset", language="English"))
    target_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Target Dataset", language="English"))

    uploaded_file = IngestedFile(user_id=user.id, dataset_id=source_dataset.id, provider=IngestedFileProviderType.Local, file_path="move.txt", filename="move.txt", file_size_bytes=8, master_hash="m" * 64, physical_path="/tmp/move.txt", status="completed")
    db_session.add(uploaded_file)
    db_session.commit()

    updated = file_services.update_dataset(
        db_session,
        user,
        source_dataset.id,
        DatasetUpdate(target_dataset_id=target_dataset.id),
    )

    assert updated.id == source_dataset.id
    assert db_session.query(IngestedFile).filter(IngestedFile.dataset_id == source_dataset.id).count() == 0
    assert db_session.query(IngestedFile).filter(IngestedFile.dataset_id == target_dataset.id).count() == 1
    assert source_dataset.status == "Created"


def test_update_dataset_can_move_a_single_file_mapping_to_another_dataset(db_session: Session) -> None:
    """Dataset updates should allow moving a single file to another dataset."""
    user = User(email="single-move@example.com", username="singlemove", full_name="Single Move", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    source_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Single Source", language="English"))
    target_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Single Target", language="English"))

    uploaded_file = IngestedFile(user_id=user.id, dataset_id=source_dataset.id, provider=IngestedFileProviderType.Local, file_path="single.txt", filename="single.txt", file_size_bytes=8, master_hash="s" * 64, physical_path="/tmp/single.txt", status="completed")
    db_session.add(uploaded_file)
    db_session.commit()

    file_services.update_dataset(
        db_session,
        user,
        source_dataset.id,
        DatasetUpdate(target_dataset_id=target_dataset.id, file_id=uploaded_file.id),
    )

    assert db_session.query(IngestedFile).filter(IngestedFile.dataset_id == source_dataset.id).count() == 0
    assert db_session.query(IngestedFile).filter(IngestedFile.dataset_id == target_dataset.id).count() == 1


def test_update_dataset_can_move_a_folder_subtree_to_another_dataset(db_session: Session) -> None:
    """Dataset updates should allow moving a file/folder to another eligible dataset."""
    user = User(email="folder-move@example.com", username="foldermove", full_name="Folder Move", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    source_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Folder Source", language="English"))
    target_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Folder Target", language="English"))

    uploaded_file = IngestedFile(user_id=user.id, dataset_id=source_dataset.id, provider=IngestedFileProviderType.Local, file_path="parent/child/nested.txt", filename="nested.txt", file_size_bytes=8, master_hash="f" * 64, physical_path="/tmp/nested.txt", status="completed")
    db_session.add(uploaded_file)
    db_session.commit()

    file_services.update_dataset(
        db_session,
        user,
        source_dataset.id,
        DatasetUpdate(target_dataset_id=target_dataset.id, file_id=uploaded_file.id),
    )

    assert db_session.query(IngestedFile).filter(IngestedFile.dataset_id == source_dataset.id).count() == 0
    assert db_session.query(IngestedFile).filter(IngestedFile.dataset_id == target_dataset.id).count() == 1
    assert target_dataset.status == "Draft"


def test_update_dataset_rejects_manual_created_transition(db_session: Session) -> None:
    """Users should not be able to explicitly move a dataset back to created."""
    user = User(email="transition@example.com", username="transition", full_name="Transition User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Transition Dataset", language="English"))

    with pytest.raises(HTTPException) as exc_info:
        file_services.update_dataset(db_session, user, dataset.id, DatasetUpdate(status="Created"))

    assert exc_info.value.status_code == 400


def test_initialize_upload_rejects_completed_dataset(db_session: Session, storage_root: Path, fake_redis: FakeRedis) -> None:
    """Uploads should be rejected once the dataset is marked completed."""
    user = User(email="completed-upload@example.com", username="completedupload", full_name="Completed Upload User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Locked", status="Completed")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    payload = UploadInitRequest(dataset_id=dataset.id, filename="report.txt", filesize=1024, master_hash="b" * 64)

    with pytest.raises(HTTPException) as exc_info:
        file_services.initialize_upload(db_session, user, payload)

    assert exc_info.value.status_code == 400


def test_get_datasets_can_filter_out_completed_datasets(db_session: Session) -> None:
    """Listing datasets for uploads should optionally exclude completed datasets."""
    user = User(email="filter@example.com", username="filter", full_name="Filter User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    created_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Created Dataset", language="English"))
    completed_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Completed Dataset", language="English"))
    file_services.update_dataset(db_session, user, completed_dataset.id, DatasetUpdate(status="Completed"))

    filtered = file_services.get_datasets(db_session, user, page=1, limit=10, include_completed=False)

    assert [item.id for item in filtered] == [created_dataset.id]


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

    uploaded_file = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.Local,
        file_path="invoice.pdf",
        filename="invoice.pdf",
        file_size_bytes=12,
        master_hash="a" * 64,
        physical_path=str(storage_root / "invoice.pdf"),
        status="completed",
    )
    db_session.add(uploaded_file)
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
    uploaded_file = IngestedFile(
        user_id=user.id,
        dataset_id=first_dataset.id,
        provider=IngestedFileProviderType.Local,
        file_path="shared.txt",
        filename="shared.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="e" * 64,
        physical_path=str(file_path),
        status="completed",
    )
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    response = file_services.delete_user_upload(db_session, user, uploaded_file.id)

    assert response.file_id == uploaded_file.id
    assert not file_path.exists()
    assert db_session.query(IngestedFile).filter(IngestedFile.id == uploaded_file.id).first() is None


def test_delete_user_upload_recomputes_dataset_status_when_last_file_is_removed(db_session: Session, storage_root: Path) -> None:
    """Removing the last file from a dataset should move it back to created."""
    user = User(email="status-delete@example.com", username="statusdelete", full_name="Status Delete User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Status Cleanup", status="Draft")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    file_path = storage_root / "files" / "cleanup.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("cleanup")
    uploaded_file = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.Local,
        file_path="cleanup.txt",
        filename="cleanup.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="g" * 64,
        physical_path=str(file_path),
        status="completed",
    )
    db_session.add(uploaded_file)
    db_session.commit()

    file_services.delete_user_upload(db_session, user, uploaded_file.id)
    db_session.refresh(dataset)

    assert dataset.status == "Created"


def test_completed_dataset_status_stays_completed_after_file_removal(db_session: Session, storage_root: Path) -> None:
    """Completed datasets should remain completed even if their last file is removed."""
    user = User(email="completed-delete@example.com", username="completeddelete", full_name="Completed Delete User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Completed Cleanup", status="Completed")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    file_path = storage_root / "files" / "completed.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("completed")
    uploaded_file = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.Local,
        file_path="completed.txt",
        filename="completed.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="h" * 64,
        physical_path=str(file_path),
        status="completed",
    )
    db_session.add(uploaded_file)
    db_session.commit()

    file_services.delete_user_upload(db_session, user, uploaded_file.id)
    db_session.refresh(dataset)

    assert dataset.status == "Completed"


def test_finalize_upload_persists_source_type_for_uploaded_file(
    db_session: Session,
    storage_root: Path,
    fake_redis: FakeRedis,
) -> None:
    """Upload finalization should store the requested provider on the file record."""
    user = User(email="source-type@example.com", username="sourcetype", full_name="Source Type User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Source Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    payload = UploadInitRequest(
        dataset_id=dataset.id,
        filename="source.txt",
        filesize=1024,
        master_hash="i" * 64,
        source_type="FTP",
    )
    init_response = file_services.initialize_upload(db_session, user, payload)

    chunk_bytes = b"hello world"
    chunk_hash = file_services._hash_bytes(chunk_bytes)
    file_services.process_upload_chunk(
        db_session,
        user,
        SimpleNamespace(upload_id=init_response.upload_id, chunk_index=0, chunk_hash=chunk_hash),
        chunk_bytes,
    )

    finalize_response = file_services.finalize_upload(
        db_session,
        user,
        SimpleNamespace(upload_id=init_response.upload_id, master_hash="i" * 64),
    )

    uploaded_file = db_session.query(IngestedFile).filter(IngestedFile.id == finalize_response.file_id).one()
    assert finalize_response.status == "completed"
    assert uploaded_file.provider.value == "FTP"


def test_finalize_upload_fallbacks_when_folder_missing(
    db_session: Session,
    storage_root: Path,
    fake_redis: FakeRedis,
) -> None:
    """Upload finalization should fallback gracefully to root dataset if target folder is missing."""
    user = User(email="missing-folder@example.com", username="missingfolder", full_name="Missing Folder User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Fallback Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    payload = UploadInitRequest(
        dataset_id=dataset.id,
        filename="fallback.txt",
        filesize=1024,
        master_hash="f" * 64,
    )
    init_response = file_services.initialize_upload(db_session, user, payload)

    chunk_bytes = b"sample content"
    chunk_hash = file_services._hash_bytes(chunk_bytes)
    file_services.process_upload_chunk(
        db_session,
        user,
        SimpleNamespace(upload_id=init_response.upload_id, chunk_index=0, chunk_hash=chunk_hash),
        chunk_bytes,
    )

    finalize_response = file_services.finalize_upload(
        db_session,
        user,
        SimpleNamespace(upload_id=init_response.upload_id, master_hash="f" * 64),
    )

    assert finalize_response.status == "completed"

    file_row = db_session.query(IngestedFile).filter(
        IngestedFile.dataset_id == dataset.id,
        IngestedFile.id == finalize_response.file_id,
    ).one()
    assert file_row is not None


def test_delete_dataset_removes_attached_files_and_soft_deletes_dataset(db_session: Session, storage_root: Path) -> None:
    """Deleting a dataset should remove its attached files and mark the dataset as deleted."""
    user = User(email="dataset-owner@example.com", username="datasetowner", full_name="Dataset Owner", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Protected")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    file_path = storage_root / "files" / "protected.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("protected contents")

    uploaded_file = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.Local,
        file_path="protected.txt",
        filename="protected.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="d" * 64,
        physical_path=str(file_path),
        status="completed",
    )
    db_session.add(uploaded_file)
    db_session.commit()

    file_services.delete_dataset(db_session, user, dataset.id)

    assert db_session.query(Dataset).filter(Dataset.id == dataset.id).first() is None
    assert db_session.query(IngestedFile).filter(IngestedFile.dataset_id == dataset.id).count() == 0
    assert db_session.query(IngestedFile).filter(IngestedFile.id == uploaded_file.id).first() is None
    assert not file_path.exists()


def test_get_dataset_tree_for_dataset_returns_nested_tree(db_session: Session) -> None:
    """A dataset should expose its linked files and folders through the tree helper."""
    user = User(email="tree@example.com", username="tree", full_name="Tree User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Tree Dataset", language="English"))
    uploaded_file = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.FTP,
        file_path="tree.txt",
        filename="tree.txt",
        file_size_bytes=4,
        master_hash="t" * 64,
        physical_path="/tmp/tree.txt",
        status="completed",
    )
    db_session.add(uploaded_file)
    db_session.commit()

    tree = file_services.get_dataset_tree_for_dataset(db_session, user, dataset.id)

    assert tree.type == "folder"
    file_children = [child for child in tree.children or [] if child.type == "file"]
    assert any(child.name == "tree.txt" for child in file_children)


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

    uploaded_file = IngestedFile(user_id=owner.id, provider=IngestedFileProviderType.Local, file_path="shared.txt", filename="shared.txt", file_size_bytes=64, master_hash="c" * 64, physical_path="/tmp/shared.txt", status="completed")
    db_session.add(uploaded_file)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        file_services.attach_file_to_dataset(db_session, other_user, dataset.id, SimpleNamespace(file_id=uploaded_file.id, relative_path=None))

    assert exc_info.value.status_code == 403


def test_dataset_file_count(db_session: Session) -> None:
    """Test that a dataset correctly tracks its linked files count."""
    user = User(email="count@example.com", username="count_user", full_name="Count User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Test Count")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    assert dataset.file_count == 0

    file1 = IngestedFile(user_id=user.id, dataset_id=dataset.id, provider=IngestedFileProviderType.Local, file_path="file1.txt", filename="file1.txt", file_size_bytes=100, master_hash="e" * 64, physical_path="/tmp/file1.txt", status="completed")
    db_session.add(file1)
    db_session.commit()

    db_session.refresh(dataset)
    assert dataset.file_count == 1

    file2 = IngestedFile(user_id=user.id, dataset_id=dataset.id, provider=IngestedFileProviderType.Local, file_path="file2.txt", filename="file2.txt", file_size_bytes=200, master_hash="f" * 64, physical_path="/tmp/file2.txt", status="completed")
    db_session.add(file2)
    db_session.commit()

    db_session.refresh(dataset)
    assert dataset.file_count == 2


def test_get_user_integrations(db_session: Session) -> None:
    """User integration status should reflect existing refresh tokens."""
    user = User(
        email="integrations@example.com",
        username="integrations",
        full_name="Integrations User",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        google_refresh_token="sample_google_token",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    res = file_services.get_user_integrations(user)
    assert res.google_connected is True
    assert res.microsoft_connected is False


def test_get_ingestion_job_status(db_session: Session, fake_redis: FakeRedis) -> None:
    """Ingestion job status polling should return job progress and details."""
    user = User(email="job-user@example.com", username="jobuser", full_name="Job User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Job Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    job = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.GDrive,
        file_path="cloud.pdf",
        filename="cloud.pdf",
        status="in_progress",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    res = file_services.get_ingestion_job_status(db_session, user, job.id)
    assert res.job_id == job.id
    assert res.provider == "GDrive"
    assert res.status == "in_progress"


def test_process_gdrive_ingestion_job(db_session: Session, storage_root: Path, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """Google Drive ingestion worker should stream chunks, compute hash, and record completion atomically."""
    user = User(email="gdrive-worker@example.com", username="gdriveworker", full_name="GDrive Worker", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="GDrive Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(user)

    job = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.GDrive,
        file_path="test_gdrive.txt",
        filename="test_gdrive.txt",
        status="pending",
    )
    db_session.add(job)
    db_session.commit()

    job_id = job.id
    dataset_id = dataset.id
    user_id = user.id

    monkeypatch.setattr(gdrive_service, "get_session_local", lambda: lambda: db_session)
    monkeypatch.setattr(gdrive_service, "redis_server", fake_redis)

    chunks = [b"google drive ", b"sample content"]

    gdrive_service.process_gdrive_ingestion_job(
        job_id=job_id,
        lock_key="lock:ingest:gdrive:99",
        stream_chunks_generator=iter(chunks),
        filename="test_gdrive.txt",
        total_bytes=sum(len(c) for c in chunks),
        dataset_id=dataset_id,
    )

    job_result = db_session.query(IngestedFile).filter(IngestedFile.id == job_id).one()
    assert job_result.status == "completed"
    assert job_result.master_hash is not None


def test_extract_gdrive_file_id() -> None:
    """Should correctly extract file IDs from various Google Drive URL formats and raw strings."""
    assert gdrive_service.extract_gdrive_file_id("1abc_XYZ-12345") == "1abc_XYZ-12345"
    assert gdrive_service.extract_gdrive_file_id("https://drive.google.com/file/d/1abc_XYZ-12345/view?usp=sharing") == "1abc_XYZ-12345"
    assert gdrive_service.extract_gdrive_file_id("https://drive.google.com/open?id=1abc_XYZ-12345") == "1abc_XYZ-12345"


def test_initiate_gdrive_ingestion_locks(db_session: Session, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """Initiating GDrive ingestion should acquire Redis lock and reject duplicate concurrent requests with 409."""
    user = User(email="lock-user@example.com", username="lockuser", full_name="Lock User", password_hash="hash", auth_provider="local", is_verified=True, google_refresh_token="mock_google_token")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Lock Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    monkeypatch.setattr(gdrive_service, "redis_server", fake_redis)
    monkeypatch.setattr(gdrive_service, "_get_gdrive_file_metadata", lambda file_id, token: {"name": "mock.txt", "mimeType": "text/plain", "size": "100"})

    bg_tasks = BackgroundTasks()


    response = gdrive_service.initiate_gdrive_ingestion(
        db=db_session,
        user=user,
        dataset_id=dataset.id,
        file_id_or_url="https://drive.google.com/file/d/shared_file_123/view",
        background_tasks=bg_tasks,
    )
    assert response.status == "pending"
    assert response.job_id is not None

    # Second call for the same file should hit Redis lock and raise 409 Conflict
    with pytest.raises(HTTPException) as exc_info:
        gdrive_service.initiate_gdrive_ingestion(
            db=db_session,
            user=user,
            dataset_id=dataset.id,
            file_id_or_url="shared_file_123",
            background_tasks=bg_tasks,
        )
    assert exc_info.value.status_code == 409


def test_process_sharepoint_ingestion_job(db_session: Session, storage_root: Path, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """SharePoint ingestion worker should stream chunks and complete atomically."""
    user = User(email="sp-worker@example.com", username="spworker", full_name="SP Worker", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="SP Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    job = IngestedFile(
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.Sharepoint,
        file_path="test_sp.docx",
        filename="test_sp.docx",
        status="pending",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    job_id = job.id
    dataset_id = dataset.id
    user_id = user.id

    monkeypatch.setattr(sharepoint_service, "get_session_local", lambda: lambda: db_session)
    monkeypatch.setattr(sharepoint_service, "redis_server", fake_redis)

    chunks = [b"sharepoint ", b"document content"]
    quick_xor_hash = "mock_quick_xor_hash_abc123"

    sharepoint_service.process_sharepoint_ingestion_job(
        job_id=job_id,
        lock_key="lock:ingest:sharepoint:555",
        stream_chunks_generator=iter(chunks),
        filename="test_sp.docx",
        total_bytes=sum(len(c) for c in chunks),
        user_id=user_id,
        dataset_id=dataset_id,
        provider_file_id="sp_item_555",
        quick_xor_hash=quick_xor_hash,
    )

    job_result = db_session.query(IngestedFile).filter(IngestedFile.id == job_id).one()
    assert job_result.status == "completed"
    assert job_result.master_hash is not None


def test_gdrive_native_app_export(db_session: Session, storage_root: Path, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """Native Google Workspace Apps should bypass pre-flight hash check and perform export."""
    user = User(email="native-app@example.com", username="nativeapp", full_name="Native App User", password_hash="hash", auth_provider="local", is_verified=True, google_refresh_token="mock_google_token")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Native App Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    monkeypatch.setattr(gdrive_service, "redis_server", fake_redis)
    monkeypatch.setattr(gdrive_service, "_get_gdrive_file_metadata", lambda file_id, token: {"name": "My Google Doc", "mimeType": "application/vnd.google-apps.document", "size": "0"})

    bg_tasks = BackgroundTasks()

    response = gdrive_service.initiate_gdrive_ingestion(
        db=db_session,
        user=user,
        dataset_id=dataset.id,
        file_id_or_url="doc_12345",
        background_tasks=bg_tasks,
        filename="My Google Doc",
        mime_type="application/vnd.google-apps.document",
    )

    assert response.status == "pending"

    job = db_session.query(IngestedFile).filter(IngestedFile.id == response.job_id).one()
    assert job.filename == "My Google Doc.pdf"

    job_id = job.id
    dataset_id = dataset.id
    user_id = user.id

    monkeypatch.setattr(gdrive_service, "get_session_local", lambda: lambda: db_session)

    exported_chunks = [b"%PDF-1.4 ", b"exported document stream"]

    gdrive_service.process_gdrive_ingestion_job(
        job_id=job_id,
        lock_key="lock:ingest:gdrive:doc_12345",
        stream_chunks_generator=iter(exported_chunks),
        filename="My Google Doc.pdf",
        total_bytes=sum(len(c) for c in exported_chunks),
        dataset_id=dataset_id,
        is_native_google_app=True,
        mime_type="application/vnd.google-apps.document",
    )

    job_result = db_session.query(IngestedFile).filter(IngestedFile.id == job_id).one()
    assert job_result.status == "completed"
    assert job_result.master_hash is not None


def test_encode_sharepoint_url() -> None:
    """Should correctly convert SharePoint URLs to u! prefixed urlsafe base64 sharing tokens."""
    token = sharepoint_service.encode_sharepoint_url("https://contoso.sharepoint.com/:u:/r/sites/marketing/doc.pdf")
    assert token.startswith("u!")
    assert "=" not in token


def test_initiate_sharepoint_ingestion_rosetta_hit(db_session: Session, storage_root: Path, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rosetta Stone hit should instantly finalize ingestion with 0 network or disk I/O."""
    user = User(email="rosetta-user@example.com", username="rosettauser", full_name="Rosetta User", password_hash="hash", auth_provider="local", is_verified=True, microsoft_refresh_token="mock_microsoft_token")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = Dataset(user_id=user.id, name="Rosetta Dataset")
    db_session.add(dataset)
    db_session.commit()
    db_session.refresh(dataset)

    dummy_file_id = "uploaded_file_888"
    dummy_path = storage_root / "rosetta_sample.pdf"
    dummy_path.write_bytes(b"sample file content for rosetta stone")
    master_hash = "f3a2b1c4d5e6f7a8"
    quick_hash = "learned_quick_xor_hash_777"

    uploaded_file = IngestedFile(
        id=dummy_file_id,
        user_id=user.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.Sharepoint,
        file_path="rosetta_sample.pdf",
        filename="rosetta_sample.pdf",
        file_size_bytes=len(b"sample file content for rosetta stone"),
        master_hash=master_hash,
        provider_hash=quick_hash,
        physical_path=str(dummy_path),
        status="completed",
    )
    db_session.add(uploaded_file)
    db_session.commit()

    monkeypatch.setattr(sharepoint_service, "redis_server", fake_redis)
    monkeypatch.setattr(sharepoint_service, "get_user_microsoft_access_token", lambda u: "mock_access_token")
    monkeypatch.setattr(sharepoint_service, "_get_sharepoint_item_metadata", lambda file_id, token: {"name": "rosetta_sample.pdf", "size": 100, "file": {"hashes": {"quickXorHash": "learned_quick_xor_hash_777"}}, "@microsoft.graph.downloadUrl": "https://download"})

    bg_tasks = BackgroundTasks()

    res = sharepoint_service.initiate_sharepoint_ingestion(
        db=db_session,
        user=user,
        dataset_id=dataset.id,
        file_id_or_url="sp_item_777",
        background_tasks=bg_tasks,
        quick_xor_hash=quick_hash,
    )

    assert res.status == "completed"
    assert res.is_instant_deduplicated is True
    assert "Rosetta Stone zero-I/O" in res.message

    job = db_session.query(IngestedFile).filter(IngestedFile.id == res.job_id).one()
    assert job.status == "completed"
    assert job.progress_percentage == 100
    assert job.file_id == dummy_file_id







