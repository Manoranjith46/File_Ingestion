from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services import file_services
from schemas.file_schema import UploadChunkRequest, UploadFinalizeRequest, UploadInitRequest
from utils.errors import ChunkValidationError, UploadSessionNotFoundError, UploadOwnershipError
from models.auth_model import Base, User


def _make_upload_init_request(filename: str = "a.txt") -> UploadInitRequest:
    return UploadInitRequest(dataset_id="ds-1", filename=filename, filesize=5 * 1024 * 1024, master_hash="a" * 64)


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.bits: dict[str, dict[int, int]] = {}
        self.ttls: dict[str, int] = {}

    def hset(self, key: str, mapping: dict[str, str] | None = None, **kwargs) -> None:
        self.hashes[key] = {**self.hashes.get(key, {}), **(mapping or {}), **kwargs}

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def setbit(self, key: str, offset: int, value: int) -> None:
        self.bits.setdefault(key, {})[offset] = value

    def bitcount(self, key: str) -> int:
        return sum(self.bits.get(key, {}).values())

    def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.hashes.pop(key, None)
            self.bits.pop(key, None)
            self.ttls.pop(key, None)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    redis_stub = FakeRedis()

    def stub_atomic(keys, args):
        bitmap_key, hashes_key, meta_key = keys
        idx = int(args[0])
        redis_stub.setbit(bitmap_key, idx, 1)
        redis_stub.hset(hashes_key, {str(idx): args[1]})
        redis_stub.expire(bitmap_key, int(args[2]))
        redis_stub.expire(hashes_key, int(args[2]))
        redis_stub.expire(meta_key, int(args[2]))
        return 0

    monkeypatch.setattr(file_services, "redis_server", redis_stub)
    monkeypatch.setattr(file_services, "atomic_chunk_state", stub_atomic)
    return redis_stub


@pytest.fixture()
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(file_services, "UPLOAD_ROOT", upload_root)
    monkeypatch.setattr(file_services, "PARTS_ROOT", upload_root / ".parts")
    monkeypatch.setattr(file_services, "FINAL_ROOT", upload_root / "files")
    file_services.PARTS_ROOT.mkdir(parents=True, exist_ok=True)
    file_services.FINAL_ROOT.mkdir(parents=True, exist_ok=True)
    return upload_root


@pytest.mark.parametrize(
    ("filesize", "expected_chunks"),
    [(5 * 1024 * 1024, 1), (10 * 1024 * 1024, 2), (0, 0)],
)
def test_compute_total_chunks(filesize, expected_chunks):
    assert file_services._compute_total_chunks(filesize) == expected_chunks


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [(None, []), ("", []), ("folder/../file.txt", "invalid"), ("folder/file.txt", ["folder", "file.txt"])],
)
def test_validate_relative_path(relative_path, expected):
    if expected == "invalid":
        with pytest.raises(Exception):
            file_services._validate_relative_path(relative_path)
    else:
        assert file_services._validate_relative_path(relative_path) == expected


@pytest.mark.parametrize(
    ("filename", "expected_status"),
    [("a.txt", "created"), ("b.txt", "created")],
)
def test_initialize_upload_handles_right_inputs(filename, expected_status, db_session, fake_redis, storage_root):
    user = _make_user(db_session)
    dataset = file_services.create_dataset(db_session, user, file_services.DatasetCreate(name="Chunk Set", language="English"))
    payload = _make_upload_init_request(filename)
    payload.dataset_id = dataset.id

    response = file_services.initialize_upload(db_session, user, payload)

    assert response.status == expected_status
    assert response.total_chunks == 1


def test_initialize_upload_rejects_empty_filename_via_schema_validation():
    with pytest.raises(Exception):
        UploadInitRequest(dataset_id="ds-1", filename="", filesize=1, master_hash="a" * 64)


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (UploadChunkRequest(upload_id="missing", chunk_index=0, chunk_hash="a" * 64), UploadSessionNotFoundError),
        (UploadChunkRequest(upload_id="upload-1", chunk_index=0, chunk_hash="a" * 64), UploadOwnershipError),
        (UploadChunkRequest(upload_id="upload-1", chunk_index=5, chunk_hash="a" * 64), ChunkValidationError),
    ],
)
def test_process_upload_chunk_handles_wrong_and_missing_sessions(payload, expected_error, db_session, fake_redis, monkeypatch):
    user = _make_user(db_session)
    if payload.upload_id == "upload-1":
        fake_redis.hset(file_services._meta_key(payload.upload_id), {"user_id": "other-user", "total_chunks": "2"})
        if payload.chunk_index == 5:
            fake_redis.hset(file_services._meta_key(payload.upload_id), {"user_id": user.id, "total_chunks": "2"})

    if expected_error is UploadSessionNotFoundError:
        with pytest.raises(UploadSessionNotFoundError):
            file_services.process_upload_chunk(db_session, user, payload, b"abc")
        return

    if expected_error is UploadOwnershipError:
        with pytest.raises(UploadOwnershipError):
            file_services.process_upload_chunk(db_session, user, payload, b"abc")
        return

    with pytest.raises(ChunkValidationError):
        file_services.process_upload_chunk(db_session, user, payload, b"abc")


def test_finalize_upload_rejects_missing_session(db_session, fake_redis):
    user = _make_user(db_session)
    payload = UploadFinalizeRequest(upload_id="missing", master_hash="a" * 64)

    with pytest.raises(UploadSessionNotFoundError):
        file_services.finalize_upload(db_session, user, payload)


def _make_user(db_session: Session) -> User:
    user = User(
        id="user-1",
        email="u@example.com",
        username="u",
        password_hash="hash",
        auth_provider="local",
        is_verified=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user
