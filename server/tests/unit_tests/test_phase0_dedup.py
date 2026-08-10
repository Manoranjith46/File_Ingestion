"""
Phase 0 — Unit Tests: Virtual Deduplication & Enum Standardization
===================================================================
Covers:
  - Filename collision detection in ``initialize_upload``
  - ``auto_rename`` flag producing ``report (1).pdf`` pattern
  - No collision across different datasets and folders
  - ``_generate_rename_suggestion`` helper
  - ``"Local"`` accepted in ``UploadedFileSourceType`` and schemas
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-p0")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-p0")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("MAX_ACTIVE_SESSIONS", "3")

from models.auth_model import Base as AuthBase, User  # noqa: E402
from models.file_model import (  # noqa: E402
    Base as FileBase,
    Dataset,
    DatasetFolderFilesMapping,
    Folder,
    UploadedFile,
    UploadedFileSourceType,
)
from schemas.file_schema import UploadInitRequest  # noqa: E402
from services import file_services  # noqa: E402
from services.file_services import (  # noqa: E402
    _generate_rename_suggestion,
    create_dataset,
    initialize_upload,
)
from schemas.file_schema import DatasetCreate  # noqa: E402
from utils.errors import FilenameCollisionError  # noqa: E402


# ===========================================================================
# FakeRedis (minimal — same pattern as test_upload.py)
# ===========================================================================

class FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict] = {}
        self._bits: dict[str, dict[int, int]] = {}
        self._strings: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    def hset(self, key, mapping=None, **kw):
        d = self._hashes.setdefault(key, {})
        if mapping:
            d.update(mapping)
        d.update(kw)

    def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    def hget(self, key, field):
        return self._hashes.get(key, {}).get(field)

    def get(self, key):
        return self._strings.get(key)

    def set(self, key, value, **kw):
        self._strings[key] = str(value)

    def getbit(self, key, offset):
        return self._bits.get(key, {}).get(offset, 0)

    def setbit(self, key, offset, value):
        self._bits.setdefault(key, {})[offset] = value

    def bitcount(self, key):
        return sum(self._bits.get(key, {}).values())

    def expire(self, key, seconds):
        self._ttls[key] = seconds

    def delete(self, *keys):
        for k in keys:
            self._hashes.pop(k, None)
            self._bits.pop(k, None)
            self._strings.pop(k, None)


# ===========================================================================
# Helpers
# ===========================================================================

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


HASH_A = _sha256(b"file-content-a")
HASH_B = _sha256(b"file-content-b")
HASH_C = _sha256(b"file-content-c")
HASH_D = _sha256(b"file-content-d")
HASH_E = _sha256(b"file-content-e")


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def fake_redis():
    return FakeRedis()


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
        engine.dispose()


@pytest.fixture()
def user(db_session: Session) -> User:
    u = User(
        id="user-p0",
        email="phase0@example.com",
        username="phase0user",
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
def other_user(db_session: Session) -> User:
    u = User(
        id="user-p0-other",
        email="phase0other@example.com",
        username="phase0other",
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
    return create_dataset(db_session, user, DatasetCreate(name="Phase0 DS", language="English"))


@pytest.fixture()
def dataset_b(db_session: Session, user: User) -> Dataset:
    return create_dataset(db_session, user, DatasetCreate(name="Phase0 DS-B", language="English"))


def _seed_existing_file(
    db_session: Session,
    user: User,
    dataset: Dataset,
    folder: Folder | None,
    filename: str,
    master_hash: str,
) -> UploadedFile:
    """Insert an UploadedFile + DatasetFolderFilesMapping to simulate a prior upload."""
    uf = UploadedFile(
        filename=filename,
        file_size_bytes=1024,
        master_hash=master_hash,
        physical_path=f"/dev/null/{filename}",
    )
    db_session.add(uf)
    db_session.flush()

    mapping = DatasetFolderFilesMapping(
        dataset_id=dataset.id,
        folder_id=folder.id if folder else None,
        file_id=uf.id,
        user_id=user.id,
    )
    db_session.add(mapping)
    db_session.commit()
    db_session.refresh(uf)
    return uf


def _seed_folder(db_session: Session, user: User, name: str) -> Folder:
    """Create a folder for the user."""
    f = Folder(user_id=user.id, name=name, parent_id=None)
    db_session.add(f)
    db_session.commit()
    db_session.refresh(f)
    return f


# ===========================================================================
# PURE HELPER TESTS — _generate_rename_suggestion
# ===========================================================================


class TestGenerateRenameSuggestion:
    """Tests for the _generate_rename_suggestion helper function."""

    def test_basic_rename(self) -> None:
        """When 'report.pdf' exists, suggestion is 'report (1).pdf'."""
        result = _generate_rename_suggestion("report.pdf", {"report.pdf"})
        assert result == "report (1).pdf"

    def test_sequential_rename(self) -> None:
        """When 'report.pdf' and 'report (1).pdf' both exist, suggestion is 'report (2).pdf'."""
        result = _generate_rename_suggestion(
            "report.pdf", {"report.pdf", "report (1).pdf"}
        )
        assert result == "report (2).pdf"

    def test_no_extension(self) -> None:
        """Files without extension still get renamed properly."""
        result = _generate_rename_suggestion("README", {"README"})
        assert result == "README (1)"

    def test_multi_dot_extension(self) -> None:
        """Only the last extension suffix is preserved."""
        result = _generate_rename_suggestion("data.tar.gz", {"data.tar.gz"})
        assert result == "data.tar (1).gz"

    def test_deeply_colliding(self) -> None:
        """When many sequential renames exist, the next free number is found."""
        existing = {"log.txt"} | {f"log ({i}).txt" for i in range(1, 10)}
        result = _generate_rename_suggestion("log.txt", existing)
        assert result == "log (10).txt"


# ===========================================================================
# COLLISION DETECTION TESTS — initialize_upload
# ===========================================================================


class TestFilenameCollision:
    """Test virtual deduplication (filename collision) in initialize_upload."""

    def test_409_on_name_collision(
        self, db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """Same filename + same dataset + same folder (root) → 409 Conflict."""
        _seed_existing_file(db_session, user, dataset, None, "report.pdf", HASH_A)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            payload = UploadInitRequest(
                dataset_id=dataset.id,
                filename="report.pdf",
                filesize=2048,
                master_hash=HASH_B,
                auto_rename=False,
            )

            with pytest.raises(FilenameCollisionError) as exc_info:
                initialize_upload(db_session, user, payload)

            assert exc_info.value.http_status == 409
            assert exc_info.value.details["conflict_type"] == "name_exists"
            assert "report.pdf" in exc_info.value.details["conflicting_files"]
            assert exc_info.value.details["suggestion"] == "report (1).pdf"

    def test_409_response_structure(
        self, db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """Verify the full response structure of the 409 Conflict."""
        _seed_existing_file(db_session, user, dataset, None, "data.csv", HASH_A)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            payload = UploadInitRequest(
                dataset_id=dataset.id,
                filename="data.csv",
                filesize=512,
                master_hash=HASH_C,
            )

            with pytest.raises(FilenameCollisionError) as exc_info:
                initialize_upload(db_session, user, payload)

            err = exc_info.value
            assert err.public_message == "Filename collision detected"
            assert err.details["suggestion"] == "data (1).csv"

    def test_auto_rename_true_succeeds(
        self, db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """auto_rename=True on collision → upload proceeds with renamed file."""
        _seed_existing_file(db_session, user, dataset, None, "report.pdf", HASH_A)

        (tmp_path / ".parts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files" / "Local").mkdir(parents=True, exist_ok=True)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            payload = UploadInitRequest(
                dataset_id=dataset.id,
                filename="report.pdf",
                filesize=2048,
                master_hash=HASH_B,
                auto_rename=True,
            )

            result = initialize_upload(db_session, user, payload)

            assert result.status == "created"
            assert result.total_chunks > 0

            # Verify the renamed filename is stored in Redis
            meta = fake_redis.hgetall(f"upload:{result.upload_id}:meta")
            assert meta["filename"] == "report (1).pdf"

    def test_auto_rename_sequential(
        self, db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """auto_rename with existing (1) already taken → produces (2)."""
        _seed_existing_file(db_session, user, dataset, None, "report.pdf", HASH_A)
        _seed_existing_file(db_session, user, dataset, None, "report (1).pdf", HASH_B)

        (tmp_path / ".parts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files" / "Local").mkdir(parents=True, exist_ok=True)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            payload = UploadInitRequest(
                dataset_id=dataset.id,
                filename="report.pdf",
                filesize=2048,
                master_hash=HASH_C,
                auto_rename=True,
            )

            result = initialize_upload(db_session, user, payload)
            meta = fake_redis.hgetall(f"upload:{result.upload_id}:meta")
            assert meta["filename"] == "report (2).pdf"

    def test_no_collision_different_dataset(
        self, db_session: Session, user: User, dataset: Dataset, dataset_b: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """Same filename but different dataset → no collision."""
        _seed_existing_file(db_session, user, dataset, None, "report.pdf", HASH_A)

        (tmp_path / ".parts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files" / "Local").mkdir(parents=True, exist_ok=True)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            payload = UploadInitRequest(
                dataset_id=dataset_b.id,
                filename="report.pdf",
                filesize=2048,
                master_hash=HASH_B,
            )

            result = initialize_upload(db_session, user, payload)
            assert result.status == "created"

    def test_no_collision_different_folder(
        self, db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """Same filename + same dataset but different folder → no collision."""
        folder_a = _seed_folder(db_session, user, "folderA")
        _seed_existing_file(db_session, user, dataset, folder_a, "report.pdf", HASH_A)

        (tmp_path / ".parts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files" / "Local").mkdir(parents=True, exist_ok=True)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            # Upload to root (no relative_path) — no collision with folderA
            payload = UploadInitRequest(
                dataset_id=dataset.id,
                filename="report.pdf",
                filesize=2048,
                master_hash=HASH_B,
            )

            result = initialize_upload(db_session, user, payload)
            assert result.status == "created"

    def test_collision_in_subfolder(
        self, db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """Collision detected inside a subfolder (via relative_path)."""
        folder_a = _seed_folder(db_session, user, "docs")
        _seed_existing_file(db_session, user, dataset, folder_a, "report.pdf", HASH_A)

        (tmp_path / ".parts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files" / "Local").mkdir(parents=True, exist_ok=True)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            payload = UploadInitRequest(
                dataset_id=dataset.id,
                filename="report.pdf",
                filesize=2048,
                master_hash=HASH_B,
                relative_path="docs/report.pdf",
            )

            with pytest.raises(FilenameCollisionError):
                initialize_upload(db_session, user, payload)

    def test_no_collision_new_file(
        self, db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_path: Path
    ) -> None:
        """Completely new filename → no collision, normal flow."""
        _seed_existing_file(db_session, user, dataset, None, "existing.pdf", HASH_A)

        (tmp_path / ".parts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files").mkdir(parents=True, exist_ok=True)
        (tmp_path / "files" / "Local").mkdir(parents=True, exist_ok=True)

        with patch.object(file_services, "redis_server", fake_redis), \
             patch.object(file_services, "UPLOAD_ROOT", tmp_path), \
             patch.object(file_services, "PARTS_ROOT", tmp_path / ".parts"), \
             patch.object(file_services, "FINAL_ROOT", tmp_path / "files"), \
             patch.object(file_services, "LOCAL_STORAGE_DIR", tmp_path / "files" / "Local"):

            payload = UploadInitRequest(
                dataset_id=dataset.id,
                filename="brand_new.pdf",
                filesize=2048,
                master_hash=HASH_B,
            )

            result = initialize_upload(db_session, user, payload)
            assert result.status == "created"


# ===========================================================================
# ENUM STANDARDIZATION TESTS
# ===========================================================================


class TestEnumStandardization:
    """Test that the UploadedFileSourceType enum contains the standardized values."""

    def test_local_enum_member_exists(self) -> None:
        """'Local' is a valid member of UploadedFileSourceType."""
        assert UploadedFileSourceType.Local.value == "Local"

    def test_ftp_enum_member_exists(self) -> None:
        """'FTP' is a valid member of UploadedFileSourceType."""
        assert UploadedFileSourceType.FTP.value == "FTP"

    def test_gdrive_enum_member_exists(self) -> None:
        """'GDrive' is a valid member of UploadedFileSourceType."""
        assert UploadedFileSourceType.GDrive.value == "GDrive"

    def test_sharepoint_enum_member_exists(self) -> None:
        """'Sharepoint' is a valid member of UploadedFileSourceType."""
        assert UploadedFileSourceType.Sharepoint.value == "Sharepoint"

    def test_all_four_members(self) -> None:
        """Exactly four members exist in the enum."""
        members = [m.value for m in UploadedFileSourceType]
        assert sorted(members) == sorted(["FTP", "Local", "GDrive", "Sharepoint"])

    def test_local_in_pydantic_schema(self) -> None:
        """'Local' is accepted as source_type in UploadInitRequest."""
        payload = UploadInitRequest(
            dataset_id="test-ds",
            filename="file.txt",
            filesize=1024,
            master_hash="a" * 64,
            source_type="Local",
        )
        assert payload.source_type == "Local"

    def test_auto_rename_field_in_schema(self) -> None:
        """auto_rename defaults to False in UploadInitRequest."""
        payload = UploadInitRequest(
            dataset_id="test-ds",
            filename="file.txt",
            filesize=1024,
            master_hash="a" * 64,
        )
        assert payload.auto_rename is False

    def test_auto_rename_field_true(self) -> None:
        """auto_rename can be set to True."""
        payload = UploadInitRequest(
            dataset_id="test-ds",
            filename="file.txt",
            filesize=1024,
            master_hash="a" * 64,
            auto_rename=True,
        )
        assert payload.auto_rename is True
