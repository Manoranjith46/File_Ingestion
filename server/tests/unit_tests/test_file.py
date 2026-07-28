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
from schemas.file_schema import DatasetCreate, DatasetUpdate, UploadInitRequest
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

    with pytest.raises(HTTPException) as exc_info:
        file_services.update_dataset(db_session, user, created.id, DatasetUpdate(status="In Progress"))
    assert exc_info.value.status_code == 400

    completed = file_services.update_dataset(db_session, user, created.id, DatasetUpdate(status="Completed"))
    assert completed.status == "Completed"


def test_dataset_status_auto_moves_to_draft_for_any_file(db_session: Session) -> None:
    """Adding files to a draft dataset should keep it in Draft until completion."""
    user = User(email="multi@example.com", username="multi", full_name="Multi User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Multi Dataset", language="English"))
    first_file = UploadedFile(filename="a.txt", file_size_bytes=1, master_hash="1" * 64, physical_path="/tmp/a.txt")
    second_file = UploadedFile(filename="b.txt", file_size_bytes=1, master_hash="2" * 64, physical_path="/tmp/b.txt")
    db_session.add_all([first_file, second_file])
    db_session.commit()
    db_session.refresh(first_file)
    db_session.refresh(second_file)

    db_session.add(
        DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=first_file.id, user_id=user.id)
    )
    db_session.commit()

    file_services.attach_file_to_dataset(db_session, user, dataset.id, SimpleNamespace(file_id=first_file.id, relative_path=None))
    db_session.refresh(dataset)
    assert dataset.status == "Draft"

    db_session.add(
        DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=second_file.id, user_id=user.id)
    )
    db_session.commit()

    file_services.attach_file_to_dataset(db_session, user, dataset.id, SimpleNamespace(file_id=second_file.id, relative_path=None))
    db_session.refresh(dataset)
    assert dataset.status == "Draft"


def test_update_dataset_can_move_mappings_to_another_dataset(db_session: Session) -> None:
    """Dataset updates should allow moving file mappings to another eligible dataset."""
    user = User(email="move@example.com", username="move", full_name="Move User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    source_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Source Dataset", language="English"))
    target_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Target Dataset", language="English"))

    uploaded_file = UploadedFile(filename="move.txt", file_size_bytes=8, master_hash="m" * 64, physical_path="/tmp/move.txt")
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=source_dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    updated = file_services.update_dataset(
        db_session,
        user,
        source_dataset.id,
        DatasetUpdate(target_dataset_id=target_dataset.id),
    )

    assert updated.id == source_dataset.id
    assert db_session.query(DatasetFolderFilesMapping).filter(
        DatasetFolderFilesMapping.dataset_id == source_dataset.id,
        DatasetFolderFilesMapping.file_id == uploaded_file.id,
    ).count() == 0
    assert db_session.query(DatasetFolderFilesMapping).filter(
        DatasetFolderFilesMapping.dataset_id == target_dataset.id,
        DatasetFolderFilesMapping.file_id == uploaded_file.id,
    ).count() == 1
    assert source_dataset.status == "Draft"


def test_update_dataset_can_move_a_single_file_mapping_to_another_dataset(db_session: Session) -> None:
    """Dataset updates should allow moving a single file mapping to another dataset."""
    user = User(email="single-move@example.com", username="singlemove", full_name="Single Move", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    source_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Single Source", language="English"))
    target_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Single Target", language="English"))

    uploaded_file = UploadedFile(filename="single.txt", file_size_bytes=8, master_hash="s" * 64, physical_path="/tmp/single.txt")
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=source_dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    file_services.update_dataset(
        db_session,
        user,
        source_dataset.id,
        DatasetUpdate(target_dataset_id=target_dataset.id, file_id=uploaded_file.id),
    )

    assert db_session.query(DatasetFolderFilesMapping).filter(
        DatasetFolderFilesMapping.dataset_id == source_dataset.id,
        DatasetFolderFilesMapping.file_id == uploaded_file.id,
    ).count() == 0
    assert db_session.query(DatasetFolderFilesMapping).filter(
        DatasetFolderFilesMapping.dataset_id == target_dataset.id,
        DatasetFolderFilesMapping.file_id == uploaded_file.id,
    ).count() == 1


def test_update_dataset_can_move_a_folder_subtree_to_another_dataset(db_session: Session) -> None:
    """Dataset updates should allow moving a folder subtree to another eligible dataset."""
    user = User(email="folder-move@example.com", username="foldermove", full_name="Folder Move", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    source_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Folder Source", language="English"))
    target_dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Folder Target", language="English"))

    parent_folder = Folder(user_id=user.id, name="parent")
    db_session.add(parent_folder)
    db_session.flush()
    child_folder = Folder(user_id=user.id, name="child", parent_id=parent_folder.id)
    db_session.add(child_folder)
    db_session.commit()
    db_session.refresh(parent_folder)
    db_session.refresh(child_folder)

    uploaded_file = UploadedFile(filename="nested.txt", file_size_bytes=8, master_hash="f" * 64, physical_path="/tmp/nested.txt")
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=source_dataset.id, folder_id=child_folder.id, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    file_services.update_dataset(
        db_session,
        user,
        source_dataset.id,
        DatasetUpdate(target_dataset_id=target_dataset.id, folder_id=parent_folder.id),
    )

    assert db_session.query(DatasetFolderFilesMapping).filter(
        DatasetFolderFilesMapping.dataset_id == source_dataset.id,
        DatasetFolderFilesMapping.file_id == uploaded_file.id,
    ).count() == 0
    assert db_session.query(DatasetFolderFilesMapping).filter(
        DatasetFolderFilesMapping.dataset_id == target_dataset.id,
        DatasetFolderFilesMapping.file_id == uploaded_file.id,
    ).count() == 1
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
    uploaded_file = UploadedFile(
        filename="cleanup.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="g" * 64,
        physical_path=str(file_path),
    )
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
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
    uploaded_file = UploadedFile(
        filename="completed.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="h" * 64,
        physical_path=str(file_path),
    )
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    file_services.delete_user_upload(db_session, user, uploaded_file.id)
    db_session.refresh(dataset)

    assert dataset.status == "Completed"


def test_finalize_upload_persists_source_type_for_uploaded_file(
    db_session: Session,
    storage_root: Path,
    fake_redis: FakeRedis,
) -> None:
    """Upload finalization should store the requested source_type on the physical file record."""
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

    uploaded_file = db_session.query(UploadedFile).filter(UploadedFile.id == finalize_response.file_id).one()
    assert finalize_response.status == "completed"
    assert uploaded_file.source_type == "FTP"


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

    uploaded_file = UploadedFile(
        filename="protected.txt",
        file_size_bytes=file_path.stat().st_size,
        master_hash="d" * 64,
        physical_path=str(file_path),
    )
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    response = file_services.delete_dataset(db_session, user, dataset.id)

    assert response is None
    assert db_session.query(Dataset).filter(Dataset.id == dataset.id).first() is None
    assert db_session.query(DatasetFolderFilesMapping).filter(DatasetFolderFilesMapping.dataset_id == dataset.id).count() == 0
    assert db_session.query(UploadedFile).filter(UploadedFile.id == uploaded_file.id).first() is None
    assert not file_path.exists()


def test_get_dataset_tree_for_dataset_returns_nested_tree(db_session: Session) -> None:
    """A dataset should expose its linked files and folders through the tree helper."""
    user = User(email="tree@example.com", username="tree", full_name="Tree User", password_hash="hash", auth_provider="local", is_verified=True)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    dataset = file_services.create_dataset(db_session, user, DatasetCreate(name="Tree Dataset", language="English"))
    uploaded_file = UploadedFile(filename="tree.txt", file_size_bytes=4, master_hash="t" * 64, physical_path="/tmp/tree.txt")
    db_session.add(uploaded_file)
    db_session.commit()
    db_session.refresh(uploaded_file)

    mapping = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id)
    db_session.add(mapping)
    db_session.commit()

    tree = file_services.get_dataset_tree_for_dataset(db_session, user, dataset.id)

    assert tree.type == "folder"
    assert any(child.type == "file" and child.name == "tree.txt" for child in tree.children)


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

    # Initially file count should be 0
    assert dataset.file_count == 0

    # Add a file mapping
    file1 = UploadedFile(filename="file1.txt", file_size_bytes=100, master_hash="e" * 64, physical_path="/tmp/file1.txt")
    db_session.add(file1)
    db_session.commit()
    db_session.refresh(file1)

    mapping1 = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=file1.id, user_id=user.id)
    db_session.add(mapping1)
    db_session.commit()

    # Refresh dataset mappings
    db_session.refresh(dataset)
    assert dataset.file_count == 1

    # Add another file mapping
    file2 = UploadedFile(filename="file2.txt", file_size_bytes=200, master_hash="f" * 64, physical_path="/tmp/file2.txt")
    db_session.add(file2)
    db_session.commit()
    db_session.refresh(file2)

    mapping2 = DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=file2.id, user_id=user.id)
    db_session.add(mapping2)
    db_session.commit()

    db_session.refresh(dataset)
    assert dataset.file_count == 2
