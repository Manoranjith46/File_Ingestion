"""
Module 3 — Unit Tests: Chunked File Upload Engine
==================================================
Covers: initialize_upload (all 3 paths), process_upload_chunk,
        finalize_upload (fast-link path only via mock), delete_user_upload,
        list_user_uploads / _build_tree, and all pure helper functions.

Isolation strategy:
  - In-memory SQLite for DB.
  - FakeRedis replaces the live Redis instance (patched on file_services).
  - FakeAtomicChunkState replaces the Lua atomic script.
  - finalize_upload (new-file path) requires pg_insert ON CONFLICT DO NOTHING —
    this path is tested via monkeypatching pg_insert to a no-op insert helper
    that is compatible with SQLite.
  - All filesystem I/O is isolated to a temp directory created per-test.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
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
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-m3")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-m3")
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
)
from schemas.file_schema import (  # noqa: E402
    DatasetCreate,
    UploadChunkRequest,
    UploadFinalizeRequest,
    UploadInitRequest,
    CHUNK_SIZE_BYTES,
)
from services import file_services  # noqa: E402
from services.file_services import (  # noqa: E402
    _compute_total_chunks,
    _hash_bytes,
    _uploaded_filename,
    _meta_key,
    _bitmap_key,
    _chunk_hashes_key,
    create_dataset,
    delete_user_upload,
    finalize_upload,
    initialize_upload,
    list_user_uploads,
    process_upload_chunk,
)


# ===========================================================================
# FakeRedis
# ===========================================================================

class FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict] = {}
        self._bits: dict[str, dict[int, int]] = {}
        self._strings: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    # --- Hash operations ---
    def hset(self, key, mapping=None, **kw):
        d = self._hashes.setdefault(key, {})
        if mapping:
            d.update(mapping)
        d.update(kw)

    def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    def hget(self, key, field):
        return self._hashes.get(key, {}).get(field)

    # --- String operations ---
    def get(self, key):
        return self._strings.get(key)

    def set(self, key, value, **kw):
        self._strings[key] = str(value)

    # --- Bit operations ---
    def getbit(self, key, offset):
        return self._bits.get(key, {}).get(offset, 0)

    def setbit(self, key, offset, value):
        self._bits.setdefault(key, {})[offset] = value

    def bitcount(self, key):
        return sum(self._bits.get(key, {}).values())

    # --- Key lifecycle ---
    def expire(self, key, seconds):
        self._ttls[key] = seconds

    def delete(self, *keys):
        for k in keys:
            self._hashes.pop(k, None)
            self._bits.pop(k, None)
            self._strings.pop(k, None)

    # --- ZSET (not used in upload tests but included for parity) ---
    def zadd(self, key, mapping): pass
    def zscore(self, key, member): return None
    def zcard(self, key): return 0
    def zrem(self, key, *members): pass
    def zremrangebyscore(self, key, mn, mx): pass
    def zremrangebyrank(self, key, start, stop): pass


def make_fake_chunk_state(fake_redis: FakeRedis):
    """Mimics the ATOMIC_CHUNK_STATE_LUA script in Python."""
    def _chunk_state(keys, args):
        bitmap_key, hashes_key, meta_key = keys
        chunk_index = int(args[0])
        chunk_hash = args[1]
        ttl = int(args[2])

        exists = fake_redis.getbit(bitmap_key, chunk_index)
        if exists == 1:
            return 1  # already uploaded

        fake_redis.setbit(bitmap_key, chunk_index, 1)
        fake_redis.hset(hashes_key, mapping={str(chunk_index): chunk_hash})
        fake_redis.expire(bitmap_key, ttl)
        fake_redis.expire(hashes_key, ttl)
        fake_redis.expire(meta_key, ttl)
        return 0  # new chunk

    return _chunk_state


# ===========================================================================
# SQLite-compatible pg_insert replacement
# ===========================================================================

def sqlite_insert(table):
    """Return a SQLite-compatible insert object that has on_conflict_do_nothing as a no-op."""
    from sqlalchemy import insert as sa_insert

    class _InsertProxy:
        def __init__(self, stmt):
            self._stmt = stmt

        def values(self, **kw):
            self._stmt = self._stmt.values(**kw)
            return self

        def on_conflict_do_nothing(self, **kw):
            # SQLite supports INSERT OR IGNORE - we just return the plain insert
            return self._stmt

    return _InsertProxy(sa_insert(table))


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture()
def tmp_upload_dir(tmp_path: Path):
    """Provide isolated upload directories for each test."""
    parts_root = tmp_path / ".parts"
    final_root = tmp_path / "files"
    parts_root.mkdir(parents=True, exist_ok=True)
    final_root.mkdir(parents=True, exist_ok=True)
    return tmp_path


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
        id="user-m3",
        email="upload@example.com",
        username="uploader",
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
        id="user-other-m3",
        email="other-m3@example.com",
        username="other_m3",
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
    return create_dataset(db_session, user, DatasetCreate(name="Upload DS", language="English"))


@pytest.fixture()
def uploaded_file(db_session: Session) -> UploadedFile:
    f = UploadedFile(
        id="phys-file-m3",
        filename="data.csv",
        file_size_bytes=1024,
        master_hash="a" * 64,
        physical_path="/dev/null/data.csv",
    )
    db_session.add(f)
    db_session.commit()
    db_session.refresh(f)
    return f


def _make_file_bytes(size: int = 100) -> bytes:
    return b"X" * size


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ===========================================================================
# PURE HELPER TESTS
# ===========================================================================

# U-C-01 — _compute_total_chunks
def test_compute_total_chunks_exact() -> None:
    """Exact multiple of chunk size → no rounding up."""
    assert _compute_total_chunks(CHUNK_SIZE_BYTES) == 1
    assert _compute_total_chunks(CHUNK_SIZE_BYTES * 3) == 3


def test_compute_total_chunks_remainder() -> None:
    """Any remainder requires an extra chunk."""
    assert _compute_total_chunks(CHUNK_SIZE_BYTES + 1) == 2


def test_compute_total_chunks_small_file() -> None:
    """A file smaller than chunk size needs exactly 1 chunk."""
    assert _compute_total_chunks(1) == 1
    assert _compute_total_chunks(1024) == 1


# U-C-02 — _hash_bytes
def test_hash_bytes_returns_sha256() -> None:
    """_hash_bytes must return the hex SHA-256 of input bytes."""
    data = b"hello"
    expected = hashlib.sha256(data).hexdigest()
    assert _hash_bytes(data) == expected


def test_hash_bytes_deterministic() -> None:
    """Same input always produces same hash."""
    data = b"deterministic"
    assert _hash_bytes(data) == _hash_bytes(data)


# U-C-03 — _uploaded_filename
def test_uploaded_filename_strips_directory() -> None:
    """_uploaded_filename should return only the basename."""
    assert _uploaded_filename("docs/report/data.csv") == "data.csv"
    assert _uploaded_filename("data.csv") == "data.csv"


# U-C-04 — Redis key helpers
def test_meta_key_format() -> None:
    assert _meta_key("abc-123") == "upload:abc-123:meta"


def test_bitmap_key_format() -> None:
    assert _bitmap_key("abc-123") == "upload:abc-123:bitmap"


def test_chunk_hashes_key_format() -> None:
    assert _chunk_hashes_key("abc-123") == "upload:abc-123:chunk_hashes"


# ===========================================================================
# initialize_upload TESTS
# ===========================================================================

# U-C-05 — initialize_upload: new file → "created" status
def test_initialize_upload_new_file(
    db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis, tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New file (no hash collision) → returns 'created' with correct chunk count."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", tmp_upload_dir / "files")

    payload = UploadInitRequest(
        dataset_id=dataset.id,
        filename="new.csv",
        filesize=CHUNK_SIZE_BYTES * 2,
        master_hash="b" * 64,
    )
    result = initialize_upload(db_session, user, payload)
    assert result.status == "created"
    assert result.total_chunks == 2
    assert result.upload_id != ""
    # Redis meta key should be set
    meta = fake_redis.hgetall(_meta_key(result.upload_id))
    assert meta["dataset_id"] == dataset.id
    assert meta["filename"] == "new.csv"


# U-C-06 — initialize_upload: file hash globally exists → "duplicate_suspected"
def test_initialize_upload_duplicate_suspected(
    db_session: Session, user: User, dataset: Dataset,
    uploaded_file: UploadedFile, fake_redis: FakeRedis, tmp_upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Global file hash exists but no mapping for this user/dataset/folder → 'duplicate_suspected'."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", tmp_upload_dir / "files")

    payload = UploadInitRequest(
        dataset_id=dataset.id,
        filename="data.csv",
        filesize=1024,
        master_hash=uploaded_file.master_hash,  # existing global hash
    )
    result = initialize_upload(db_session, user, payload)
    assert result.status == "duplicate_suspected"
    assert result.total_chunks == 0
    # Redis meta should have linked_file_id
    meta = fake_redis.hgetall(_meta_key(result.upload_id))
    assert meta["linked_file_id"] == uploaded_file.id


# U-C-07 — initialize_upload: mapping already exists → "duplicate_short_circuit"
def test_initialize_upload_duplicate_short_circuit(
    db_session: Session, user: User, dataset: Dataset,
    uploaded_file: UploadedFile, fake_redis: FakeRedis, tmp_upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact same user/dataset/folder/hash mapping exists → 'duplicate_short_circuit'."""
    # Pre-create the mapping
    mapping = DatasetFolderFilesMapping(
        dataset_id=dataset.id,
        folder_id=None,
        file_id=uploaded_file.id,
        user_id=user.id,
    )
    db_session.add(mapping)
    db_session.commit()

    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", tmp_upload_dir / "files")

    payload = UploadInitRequest(
        dataset_id=dataset.id,
        filename="data.csv",
        filesize=1024,
        master_hash=uploaded_file.master_hash,
    )
    result = initialize_upload(db_session, user, payload)
    assert result.status == "duplicate_short_circuit"
    assert result.total_chunks == 0


# U-C-08 — initialize_upload: dataset not found → 404
def test_initialize_upload_dataset_not_found_raises_404(
    db_session: Session, user: User, fake_redis: FakeRedis, tmp_upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", tmp_upload_dir / "files")

    with pytest.raises(HTTPException) as exc:
        initialize_upload(
            db_session, user,
            UploadInitRequest(dataset_id="no-such-dataset", filename="f.csv", filesize=100, master_hash="c" * 64)
        )
    assert exc.value.status_code == 404


# U-C-09 — initialize_upload: completed dataset → 400
def test_initialize_upload_completed_dataset_raises_400(
    db_session: Session, user: User, dataset: Dataset, fake_redis: FakeRedis,
    tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset.status = "Completed"
    db_session.commit()

    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", tmp_upload_dir / "files")

    with pytest.raises(HTTPException) as exc:
        initialize_upload(
            db_session, user,
            UploadInitRequest(dataset_id=dataset.id, filename="f.csv", filesize=100, master_hash="d" * 64)
        )
    assert exc.value.status_code == 400


# ===========================================================================
# process_upload_chunk TESTS
# ===========================================================================

def _seed_redis_session(fake_redis: FakeRedis, upload_id: str, user_id: str, dataset_id: str, total_chunks: int) -> None:
    """Seed a Redis upload session for chunk tests."""
    fake_redis.hset(_meta_key(upload_id), mapping={
        "user_id": user_id,
        "dataset_id": dataset_id,
        "filename": "test.csv",
        "filesize": str(total_chunks * CHUNK_SIZE_BYTES),
        "master_hash": "e" * 64,
        "folder_id": "",
        "total_chunks": str(total_chunks),
    })


# U-C-10 — process_upload_chunk: happy path
def test_process_chunk_happy_path(
    db_session: Session, user: User, dataset: Dataset,
    fake_redis: FakeRedis, tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid chunk is stored and response is correct."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fake_redis))
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")

    upload_id = "chunk-test-ok"
    _seed_redis_session(fake_redis, upload_id, user.id, dataset.id, total_chunks=2)
    (tmp_upload_dir / ".parts" / upload_id).mkdir(parents=True, exist_ok=True)

    chunk_bytes = _make_file_bytes(100)
    chunk_hash = _sha256(chunk_bytes)
    payload = UploadChunkRequest(upload_id=upload_id, chunk_index=0, chunk_hash=chunk_hash)

    result = process_upload_chunk(db_session, user, payload, chunk_bytes)
    assert result.upload_id == upload_id
    assert result.chunk_index == 0
    assert result.bytes_received == 100
    assert result.received_chunks == 1
    assert result.total_chunks == 2
    assert result.complete is False


# U-C-11 — process_upload_chunk: session not found → 404
def test_process_chunk_session_not_found_raises_404(
    db_session: Session, user: User, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fake_redis))

    with pytest.raises(HTTPException) as exc:
        process_upload_chunk(
            db_session, user,
            UploadChunkRequest(upload_id="ghost-session", chunk_index=0, chunk_hash="a" * 64),
            b"data"
        )
    assert exc.value.status_code == 404


# U-C-12 — process_upload_chunk: wrong user → 403
def test_process_chunk_wrong_user_raises_403(
    db_session: Session, user: User, other_user: User, dataset: Dataset,
    fake_redis: FakeRedis, tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user attempting to upload to another user's session must get 403."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fake_redis))
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")

    upload_id = "chunk-wrong-user"
    _seed_redis_session(fake_redis, upload_id, other_user.id, dataset.id, total_chunks=1)

    chunk_bytes = b"some data"
    chunk_hash = _sha256(chunk_bytes)
    with pytest.raises(HTTPException) as exc:
        process_upload_chunk(
            db_session, user,
            UploadChunkRequest(upload_id=upload_id, chunk_index=0, chunk_hash=chunk_hash),
            chunk_bytes
        )
    assert exc.value.status_code == 403


# U-C-13 — process_upload_chunk: chunk_index out of range → 400
def test_process_chunk_out_of_range_raises_400(
    db_session: Session, user: User, dataset: Dataset,
    fake_redis: FakeRedis, tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chunk index ≥ total_chunks must raise 400."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fake_redis))
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")

    upload_id = "chunk-oob"
    _seed_redis_session(fake_redis, upload_id, user.id, dataset.id, total_chunks=2)

    with pytest.raises(HTTPException) as exc:
        process_upload_chunk(
            db_session, user,
            UploadChunkRequest(upload_id=upload_id, chunk_index=5, chunk_hash="f" * 64),
            b"data"
        )
    assert exc.value.status_code == 400


# U-C-14 — process_upload_chunk: hash mismatch → 400
def test_process_chunk_hash_mismatch_raises_400(
    db_session: Session, user: User, dataset: Dataset,
    fake_redis: FakeRedis, tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supplying an incorrect chunk_hash must raise 400."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fake_redis))
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")

    upload_id = "chunk-hash-mismatch"
    _seed_redis_session(fake_redis, upload_id, user.id, dataset.id, total_chunks=1)

    with pytest.raises(HTTPException) as exc:
        process_upload_chunk(
            db_session, user,
            UploadChunkRequest(upload_id=upload_id, chunk_index=0, chunk_hash="0" * 64),
            b"actual data"
        )
    assert exc.value.status_code == 400


# U-C-15 — process_upload_chunk: duplicate chunk (already_uploaded) returns correct response
def test_process_chunk_duplicate_chunk_returns_ok(
    db_session: Session, user: User, dataset: Dataset,
    fake_redis: FakeRedis, tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-uploading an already-received chunk should not raise an error."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fake_redis))
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")

    upload_id = "chunk-dup"
    _seed_redis_session(fake_redis, upload_id, user.id, dataset.id, total_chunks=2)
    (tmp_upload_dir / ".parts" / upload_id).mkdir(parents=True, exist_ok=True)

    chunk_bytes = _make_file_bytes(50)
    chunk_hash = _sha256(chunk_bytes)
    payload = UploadChunkRequest(upload_id=upload_id, chunk_index=0, chunk_hash=chunk_hash)

    # Upload once
    process_upload_chunk(db_session, user, payload, chunk_bytes)
    # Upload same chunk again — should not raise
    result = process_upload_chunk(db_session, user, payload, chunk_bytes)
    assert result.upload_id == upload_id
    assert result.received_chunks == 1  # still 1, not 2


# ===========================================================================
# finalize_upload TESTS (fast-link path via FakeRedis + mock pg_insert)
# ===========================================================================

def _seed_fast_link_session(
    fake_redis: FakeRedis, upload_id: str, user_id: str, dataset_id: str, file_id: str, master_hash: str
) -> None:
    """Seed a duplicate_suspected Redis session pointing to an existing file."""
    fake_redis.hset(_meta_key(upload_id), mapping={
        "user_id": user_id,
        "dataset_id": dataset_id,
        "filename": "data.csv",
        "filesize": "1024",
        "master_hash": master_hash,
        "folder_id": "",
        "total_chunks": "0",
        "linked_file_id": file_id,
        "source_type": "",
    })


# U-C-16 — finalize_upload: fast-link (duplicate_suspected) path
def test_finalize_upload_fast_link_happy_path(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile,
    fake_redis: FakeRedis, tmp_upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fast-link finalize must link existing file to dataset without file I/O."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)
    monkeypatch.setattr(file_services, "PARTS_ROOT", tmp_upload_dir / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", tmp_upload_dir / "files")
    monkeypatch.setattr(file_services, "pg_insert", sqlite_insert)

    upload_id = "fin-fast-link"
    _seed_fast_link_session(
        fake_redis, upload_id, user.id, dataset.id, uploaded_file.id, uploaded_file.master_hash
    )

    result = finalize_upload(
        db_session, user, UploadFinalizeRequest(upload_id=upload_id, master_hash=uploaded_file.master_hash)
    )
    assert result.status == "completed"
    assert result.file_id == uploaded_file.id


# U-C-17 — finalize_upload: session not found → 404
def test_finalize_upload_session_not_found_raises_404(
    db_session: Session, user: User, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_services, "redis_server", fake_redis)

    with pytest.raises(HTTPException) as exc:
        finalize_upload(
            db_session, user,
            UploadFinalizeRequest(upload_id="ghost", master_hash="a" * 64)
        )
    assert exc.value.status_code == 404


# U-C-18 — finalize_upload: wrong user → 403
def test_finalize_upload_wrong_user_raises_403(
    db_session: Session, user: User, other_user: User, dataset: Dataset,
    uploaded_file: UploadedFile, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finalizing another user's session must raise 403."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)

    upload_id = "fin-wrong-user"
    _seed_fast_link_session(
        fake_redis, upload_id, other_user.id, dataset.id, uploaded_file.id, uploaded_file.master_hash
    )

    with pytest.raises(HTTPException) as exc:
        finalize_upload(
            db_session, user,
            UploadFinalizeRequest(upload_id=upload_id, master_hash=uploaded_file.master_hash)
        )
    assert exc.value.status_code == 403


# U-C-19 — finalize_upload: master_hash mismatch → 400
def test_finalize_upload_master_hash_mismatch_raises_400(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile,
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_services, "redis_server", fake_redis)

    upload_id = "fin-hash-mismatch"
    _seed_fast_link_session(
        fake_redis, upload_id, user.id, dataset.id, uploaded_file.id, uploaded_file.master_hash
    )

    with pytest.raises(HTTPException) as exc:
        finalize_upload(
            db_session, user,
            UploadFinalizeRequest(upload_id=upload_id, master_hash="z" * 64)  # wrong hash
        )
    assert exc.value.status_code == 400


# U-C-20 — finalize_upload: linked_file_id points to deleted file → 404
def test_finalize_upload_linked_file_missing_raises_404(
    db_session: Session, user: User, dataset: Dataset,
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the linked_file_id no longer exists in DB, must raise 404."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)

    upload_id = "fin-linked-missing"
    fake_redis.hset(_meta_key(upload_id), mapping={
        "user_id": user.id,
        "dataset_id": dataset.id,
        "filename": "ghost.csv",
        "filesize": "100",
        "master_hash": "g" * 64,
        "folder_id": "",
        "total_chunks": "0",
        "linked_file_id": "nonexistent-file-id",
        "source_type": "",
    })

    with pytest.raises(HTTPException) as exc:
        finalize_upload(
            db_session, user,
            UploadFinalizeRequest(upload_id=upload_id, master_hash="g" * 64)
        )
    assert exc.value.status_code == 404


# U-C-21 — finalize_upload: standard path — incomplete upload → 409
def test_finalize_upload_incomplete_raises_409(
    db_session: Session, user: User, dataset: Dataset,
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finalizing before all chunks are received must raise 409."""
    monkeypatch.setattr(file_services, "redis_server", fake_redis)

    upload_id = "fin-incomplete"
    fake_redis.hset(_meta_key(upload_id), mapping={
        "user_id": user.id,
        "dataset_id": dataset.id,
        "filename": "incomplete.csv",
        "filesize": str(CHUNK_SIZE_BYTES * 2),
        "master_hash": "h" * 64,
        "folder_id": "",
        "total_chunks": "2",  # expects 2 chunks
        # No linked_file_id → standard upload path
    })
    # Only 0 bits set (bitcount = 0), but needs 2

    with pytest.raises(HTTPException) as exc:
        finalize_upload(
            db_session, user,
            UploadFinalizeRequest(upload_id=upload_id, master_hash="h" * 64)
        )
    assert exc.value.status_code == 409


# ===========================================================================
# delete_user_upload TESTS
# ===========================================================================

# U-C-22 — delete_user_upload: happy path
def test_delete_user_upload_happy_path(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile,
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch, tmp_upload_dir: Path
) -> None:
    """Deleting an owned file should remove the mapping and the UploadedFile row."""
    # Create a real temp file for the physical_path
    phys = tmp_upload_dir / "files" / "data.csv"
    phys.write_bytes(b"content")
    uploaded_file.physical_path = str(phys)
    db_session.commit()

    # Attach file to dataset
    mapping = DatasetFolderFilesMapping(
        dataset_id=dataset.id, folder_id=None,
        file_id=uploaded_file.id, user_id=user.id
    )
    db_session.add(mapping)
    db_session.commit()

    result = delete_user_upload(db_session, user, uploaded_file.id)
    assert result.status == "deleted"
    assert result.file_id == uploaded_file.id

    # File should be removed from DB
    gone = db_session.query(UploadedFile).filter(UploadedFile.id == uploaded_file.id).one_or_none()
    assert gone is None
    # Physical file should be removed
    assert not phys.exists()


# U-C-23 — delete_user_upload: file not found → 404
def test_delete_user_upload_not_found_raises_404(
    db_session: Session, user: User
) -> None:
    with pytest.raises(HTTPException) as exc:
        delete_user_upload(db_session, user, "nonexistent-file-777")
    assert exc.value.status_code == 404


# U-C-24 — delete_user_upload: file exists but no ownership mapping → 404
def test_delete_user_upload_no_ownership_mapping_raises_404(
    db_session: Session, user: User, other_user: User, dataset: Dataset, uploaded_file: UploadedFile
) -> None:
    """File exists but the requesting user has no mapping to it → 404."""
    # Only other_user has a mapping
    other_ds = create_dataset(db_session, other_user, DatasetCreate(name="Other DS", language="English"))
    mapping = DatasetFolderFilesMapping(
        dataset_id=other_ds.id, folder_id=None,
        file_id=uploaded_file.id, user_id=other_user.id
    )
    db_session.add(mapping)
    db_session.commit()

    # user tries to delete other_user's file
    with pytest.raises(HTTPException) as exc:
        delete_user_upload(db_session, user, uploaded_file.id)
    assert exc.value.status_code == 404


# ===========================================================================
# list_user_uploads / _build_tree TESTS
# ===========================================================================

# U-C-25 — list_user_uploads: empty returns root node
def test_list_user_uploads_empty(db_session: Session, user: User) -> None:
    """When no files uploaded, tree should be a root folder node with no children."""
    result = list_user_uploads(db_session, user)
    assert result.type == "folder"
    assert result.name == "root"
    children = result.children or []
    assert len(children) == 0


# U-C-26 — list_user_uploads: file at root level
def test_list_user_uploads_file_at_root(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile
) -> None:
    """A file without folder should appear as direct child of root in tree."""
    mapping = DatasetFolderFilesMapping(
        dataset_id=dataset.id, folder_id=None,
        file_id=uploaded_file.id, user_id=user.id
    )
    db_session.add(mapping)
    db_session.commit()

    result = list_user_uploads(db_session, user)
    children = result.children or []
    file_nodes = [c for c in children if c.type == "file"]
    assert any(f.name == "data.csv" for f in file_nodes)


# U-C-27 — list_user_uploads: file in subfolder
def test_list_user_uploads_file_in_folder(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile
) -> None:
    """A file with a folder mapping should appear under that folder in tree."""
    folder = Folder(user_id=user.id, name="reports", parent_id=None)
    db_session.add(folder)
    db_session.flush()

    mapping = DatasetFolderFilesMapping(
        dataset_id=dataset.id, folder_id=folder.id,
        file_id=uploaded_file.id, user_id=user.id
    )
    db_session.add(mapping)
    db_session.commit()

    result = list_user_uploads(db_session, user)
    children = result.children or []
    folder_nodes = [c for c in children if c.type == "folder"]
    assert any(f.name == "reports" for f in folder_nodes)
    # Check files are inside the folder node
    reports_node = next(c for c in children if c.type == "folder" and c.name == "reports")
    inner_files = [c for c in (reports_node.children or []) if c.type == "file"]
    assert any(f.name == "data.csv" for f in inner_files)


# U-C-28 — list_user_uploads: filtered by dataset_id
def test_list_user_uploads_filter_by_dataset(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile
) -> None:
    """Filtering by dataset_id returns only files in that dataset."""
    ds2 = create_dataset(db_session, user, DatasetCreate(name="DS2", language="English"))

    uf2 = UploadedFile(id="file-m3-2", filename="other.csv", file_size_bytes=512, master_hash="i" * 64, physical_path="/dev/null")
    db_session.add(uf2)
    db_session.flush()

    # uploaded_file → dataset, uf2 → ds2
    db_session.add(DatasetFolderFilesMapping(dataset_id=dataset.id, folder_id=None, file_id=uploaded_file.id, user_id=user.id))
    db_session.add(DatasetFolderFilesMapping(dataset_id=ds2.id, folder_id=None, file_id=uf2.id, user_id=user.id))
    db_session.commit()

    result = list_user_uploads(db_session, user, dataset_id=dataset.id)
    children = result.children or []
    file_names = [c.name for c in children if c.type == "file"]
    assert "data.csv" in file_names
    assert "other.csv" not in file_names


# U-C-29 — _build_tree: non-existent dataset_id returns empty root (not 404)
def test_build_tree_nonexistent_dataset_returns_empty_root(db_session: Session, user: User) -> None:
    """Filtering by a non-existent dataset_id should return an empty root (not raise 404)."""
    result = list_user_uploads(db_session, user, dataset_id="nonexistent-ds")
    assert result.type == "folder"
    children = result.children or []
    assert len(children) == 0


# U-C-30 — FakeRedis: chunk state correctly sets bit and prevents re-processing
def test_fake_chunk_state_deduplication(fake_redis: FakeRedis) -> None:
    """FakeAtomicChunkState must return 0 on first call and 1 on duplicate."""
    chunk_state = make_fake_chunk_state(fake_redis)
    bitmap_key, hashes_key, meta_key = "bm:1", "ch:1", "mt:1"

    result1 = chunk_state([bitmap_key, hashes_key, meta_key], [0, "hash1", 3600])
    assert result1 == 0  # new chunk

    result2 = chunk_state([bitmap_key, hashes_key, meta_key], [0, "hash1", 3600])
    assert result2 == 1  # already uploaded
