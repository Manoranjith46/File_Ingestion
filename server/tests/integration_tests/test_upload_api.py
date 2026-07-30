"""
Module 3 — Integration Tests: Chunked File Upload Engine
=========================================================
Tests all HTTP endpoints for file upload:
  POST /v1/upload/init
  POST /v1/upload/chunk
  POST /v1/upload/finalize
  GET  /v1/uploads
  POST /v1/uploads/delete

Isolation strategy:
  - In-memory SQLite via dependency_overrides on get_db.
  - FakeRedis patched on both file_services + auth_services.
  - FakeAtomicChunkState patched on file_services.
  - pg_insert patched with a SQLite-compatible wrapper (on_conflict_do_nothing is a no-op).
  - Filesystem I/O uses pytest's tmp_path fixture — each test gets an isolated directory.
  - chunk endpoint uses multipart/form-data; helper builds the correct form.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert as sa_insert
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-m3-int")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-m3-int")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("MAX_ACTIVE_SESSIONS", "3")

from config.database import get_db  # noqa: E402
from models.auth_model import Base as AuthBase, User  # noqa: E402
from models.file_model import (  # noqa: E402
    Base as FileBase,
    Dataset,
    DatasetFolderFilesMapping,
    UploadedFile,
)
from schemas.file_schema import DatasetCreate, CHUNK_SIZE_BYTES  # noqa: E402
from services import auth_services, file_services  # noqa: E402
from services.file_services import create_dataset, _meta_key, _bitmap_key, _chunk_hashes_key  # noqa: E402
from main import app  # noqa: E402

DEV_OTP = "123456"


# ===========================================================================
# FakeRedis (same pattern as Module 2 integration tests)
# ===========================================================================

class FakeRedis:
    def __init__(self) -> None:
        self._hashes: dict = {}
        self._bits: dict = {}
        self._strings: dict = {}
        self._zsets: dict = {}

    def hset(self, key, mapping=None, **kw):
        d = self._hashes.setdefault(key, {})
        if mapping: d.update(mapping)
        d.update(kw)

    def hgetall(self, key): return dict(self._hashes.get(key, {}))
    def hget(self, key, field): return self._hashes.get(key, {}).get(field)
    def get(self, key): return self._strings.get(key)
    def set(self, key, value, **kw): self._strings[key] = str(value)

    def getbit(self, key, offset):
        return self._bits.get(key, {}).get(offset, 0)

    def setbit(self, key, offset, value):
        self._bits.setdefault(key, {})[offset] = value

    def bitcount(self, key):
        return sum(self._bits.get(key, {}).values())

    def expire(self, key, seconds): pass

    def delete(self, *keys):
        for k in keys:
            self._hashes.pop(k, None)
            self._bits.pop(k, None)
            self._strings.pop(k, None)

    # ZSET for auth_services session limiter
    def zadd(self, key, mapping): self._zsets.setdefault(key, {}).update(mapping)
    def zscore(self, key, member): return self._zsets.get(key, {}).get(member)
    def zcard(self, key): return len(self._zsets.get(key, {}))
    def zrem(self, key, *members):
        for m in members: self._zsets.get(key, {}).pop(m, None)
    def zremrangebyscore(self, key, mn, mx):
        zset = self._zsets.get(key, {})
        for m in [m for m, s in list(zset.items()) if mn <= s <= mx]: del zset[m]
    def zremrangebyrank(self, key, start, stop):
        zset = self._zsets.get(key, {})
        srt = sorted(zset.items(), key=lambda x: x[1])
        if stop < 0: stop = len(srt) + stop
        for m, _ in srt[start:stop+1]: del zset[m]


def make_fake_limiter(fr):
    def _limiter(keys, args):
        key = keys[0]; now = int(args[0]); sid = args[1]
        max_s = int(args[2]); ttl = int(args[3])
        fr.zremrangebyscore(key, float("-inf"), now - ttl)
        fr.zadd(key, {sid: float(now)})
        card = fr.zcard(key)
        if card > max_s: fr.zremrangebyrank(key, 0, card - max_s - 1)
        return 1
    return _limiter


def make_fake_chunk_state(fr: FakeRedis):
    def _chunk_state(keys, args):
        bitmap_key, hashes_key, meta_key = keys
        chunk_index = int(args[0]); chunk_hash = args[1]; ttl = int(args[2])
        if fr.getbit(bitmap_key, chunk_index) == 1:
            return 1
        fr.setbit(bitmap_key, chunk_index, 1)
        fr.hset(hashes_key, mapping={str(chunk_index): chunk_hash})
        fr.expire(bitmap_key, ttl); fr.expire(hashes_key, ttl); fr.expire(meta_key, ttl)
        return 0
    return _chunk_state


def sqlite_insert(table):
    """SQLite-compatible replacement for pg_insert used in finalize_upload."""
    class _Proxy:
        def __init__(self, stmt): self._stmt = stmt
        def values(self, **kw):
            self._stmt = self._stmt.values(**kw); return self
        def on_conflict_do_nothing(self, **kw): return self._stmt  # no-op on SQLite
    return _Proxy(sa_insert(table))


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope="module")
def _engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    AuthBase.metadata.create_all(bind=engine)
    FileBase.metadata.create_all(bind=engine)
    yield engine
    AuthBase.metadata.drop_all(bind=engine)
    FileBase.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(scope="module")
def _session_factory(_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture()
def db_session(_session_factory):
    conn = _session_factory().bind.connect()
    txn = conn.begin()
    session = Session(bind=conn)
    try:
        yield session
    finally:
        session.close()
        txn.rollback()
        conn.close()


@pytest.fixture()
def fr() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def client(db_session, fr, tmp_path, monkeypatch):
    monkeypatch.setattr(auth_services, "redis_server", fr)
    monkeypatch.setattr(auth_services, "active_session_limiter", make_fake_limiter(fr))
    monkeypatch.setattr(file_services, "redis_server", fr)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fr))
    monkeypatch.setattr(file_services, "pg_insert", sqlite_insert)

    parts = tmp_path / ".parts"
    final = tmp_path / "files"
    parts.mkdir(parents=True, exist_ok=True)
    final.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(file_services, "PARTS_ROOT", parts)
    monkeypatch.setattr(file_services, "FINAL_ROOT", final)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ===========================================================================
# Auth & Dataset helpers
# ===========================================================================

def _register_login(client: TestClient, email: str, username: str, password: str = "Pass1234!") -> str:
    client.post("/auth/signup/init", json={"email": email, "username": username, "password": password})
    client.post("/auth/signup/verify", json={"email": email, "otp_code": DEV_OTP})
    resp = client.post("/auth/login", json={"identifier": email, "password": password})
    assert resp.status_code == 200
    return resp.headers.get("authorization", "").removeprefix("Bearer ").strip()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_dataset(client: TestClient, token: str, name: str = "Upload DS") -> str:
    resp = client.post("/v1/datasets", json={"name": name, "language": "English"}, headers=_auth(token))
    assert resp.status_code == 201
    return resp.json()["id"]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seed_file_and_mapping(db_session, user_id: str, dataset_id: str) -> UploadedFile:
    """Seed a physical file row + mapping for tests that need existing files."""
    f = UploadedFile(
        id=f"seed-{user_id[:8]}",
        filename="seed.csv",
        file_size_bytes=256,
        master_hash="s" * 64,
        physical_path="/dev/null/seed.csv",
    )
    db_session.add(f)
    db_session.flush()
    m = DatasetFolderFilesMapping(dataset_id=dataset_id, folder_id=None, file_id=f.id, user_id=user_id)
    db_session.add(m)
    db_session.commit()
    return f


# ===========================================================================
# I-C-01 — POST /v1/upload/init — New file → 200 + "created"
# ===========================================================================
def test_upload_init_new_file(client: TestClient) -> None:
    """init for a completely new file must return status='created' with chunk info."""
    token = _register_login(client, "init_new@example.com", "init_new")
    ds_id = _create_dataset(client, token, "Init New DS")

    resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id,
        "filename": "newfile.csv",
        "filesize": CHUNK_SIZE_BYTES,
        "master_hash": "a" * 64,
    }, headers=_auth(token))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "created"
    assert body["total_chunks"] == 1
    assert body["chunk_size"] == CHUNK_SIZE_BYTES
    assert "upload_id" in body


# ===========================================================================
# I-C-02 — POST /v1/upload/init — Duplicate (short-circuit, mapping exists)
# ===========================================================================
def test_upload_init_duplicate_short_circuit(client: TestClient, db_session: Session) -> None:
    """If exact user/dataset/folder/hash mapping exists → 'duplicate_short_circuit'."""
    token = _register_login(client, "init_sc@example.com", "init_sc")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]
    ds_id = _create_dataset(client, token, "SC DS")

    # Seed a unique file with a distinct hash and a mapping to this dataset.
    # _seed_file_and_mapping uses hash "s"*64 which is unique to this test.
    # We then init with that same hash — service must detect the existing mapping
    # and return duplicate_short_circuit.
    unique_hash = "2" * 64
    sc_file = UploadedFile(
        id="sc-file-unique",
        filename="sc.csv",
        file_size_bytes=256,
        master_hash=unique_hash,
        physical_path="/dev/null",
    )
    db_session.add(sc_file)
    db_session.flush()
    db_session.add(DatasetFolderFilesMapping(
        dataset_id=ds_id, folder_id=None, file_id=sc_file.id, user_id=user_id
    ))
    db_session.commit()

    resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id,
        "filename": "sc.csv",
        "filesize": 256,
        "master_hash": unique_hash,
    }, headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate_short_circuit"
    assert resp.json()["total_chunks"] == 0


# ===========================================================================
# I-C-03 — POST /v1/upload/init — File globally exists but no mapping ("duplicate_suspected")
# ===========================================================================
def test_upload_init_duplicate_suspected(client: TestClient, db_session: Session) -> None:
    """File exists globally but not for this user/dataset → 'duplicate_suspected'."""
    token = _register_login(client, "init_ds@example.com", "init_ds")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]
    ds_id = _create_dataset(client, token, "DS-Suspected")

    # Seed global file (no mapping)
    gf = UploadedFile(id="global-file", filename="global.csv", file_size_bytes=100, master_hash="c" * 64, physical_path="/dev/null")
    db_session.add(gf)
    db_session.commit()

    resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id,
        "filename": "global.csv",
        "filesize": 100,
        "master_hash": "c" * 64,
    }, headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate_suspected"
    assert resp.json()["total_chunks"] == 0


# ===========================================================================
# I-C-04 — POST /v1/upload/init — Completed dataset → 400
# ===========================================================================
def test_upload_init_completed_dataset_returns_400(client: TestClient, db_session: Session) -> None:
    token = _register_login(client, "init_comp@example.com", "init_comp")
    ds_id = _create_dataset(client, token, "Comp DS")
    client.patch(f"/v1/datasets/{ds_id}", json={"status": "Completed"}, headers=_auth(token))

    resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id,
        "filename": "f.csv",
        "filesize": 100,
        "master_hash": "d" * 64,
    }, headers=_auth(token))
    assert resp.status_code == 400


# ===========================================================================
# I-C-05 — POST /v1/upload/init — Non-existent dataset → 404
# ===========================================================================
def test_upload_init_missing_dataset_returns_404(client: TestClient) -> None:
    token = _register_login(client, "init_404@example.com", "init_404")
    resp = client.post("/v1/upload/init", json={
        "dataset_id": "no-such-ds",
        "filename": "f.csv",
        "filesize": 100,
        "master_hash": "e" * 64,
    }, headers=_auth(token))
    assert resp.status_code == 404


# ===========================================================================
# I-C-06 — POST /v1/upload/init — Missing authorization → 401
# ===========================================================================
def test_upload_init_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.post("/v1/upload/init", json={
        "dataset_id": "ds", "filename": "f.csv", "filesize": 100, "master_hash": "a" * 64
    })
    assert resp.status_code == 401


# ===========================================================================
# I-C-07 — POST /v1/upload/init — Missing required fields → 422
# ===========================================================================
def test_upload_init_missing_fields_returns_422(client: TestClient) -> None:
    token = _register_login(client, "init_422@example.com", "init_422")
    resp = client.post("/v1/upload/init", json={"dataset_id": "ds"}, headers=_auth(token))
    assert resp.status_code == 422


# ===========================================================================
# I-C-08 — POST /v1/upload/chunk — Happy path
# ===========================================================================
def test_upload_chunk_happy_path(client: TestClient, fr: FakeRedis) -> None:
    """A valid chunk upload must return correct progress metadata."""
    token = _register_login(client, "chunk_ok@example.com", "chunk_ok")
    ds_id = _create_dataset(client, token, "Chunk DS")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]

    # Create session via init
    init_resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id,
        "filename": "chunk.csv",
        "filesize": CHUNK_SIZE_BYTES * 2,
        "master_hash": "f" * 64,
    }, headers=_auth(token))
    upload_id = init_resp.json()["upload_id"]

    # Upload chunk 0
    chunk_data = b"X" * 1024
    chunk_hash = _sha256(chunk_data)

    resp = client.post(
        "/v1/upload/chunk",
        data={"upload_id": upload_id, "chunk_index": "0", "chunk_hash": chunk_hash},
        files={"chunk_file": ("chunk.bin", io.BytesIO(chunk_data), "application/octet-stream")},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["upload_id"] == upload_id
    assert body["chunk_index"] == 0
    assert body["bytes_received"] == 1024
    assert body["received_chunks"] == 1
    assert body["total_chunks"] == 2
    assert body["complete"] is False


# ===========================================================================
# I-C-09 — POST /v1/upload/chunk — Session not found → 404
# ===========================================================================
def test_upload_chunk_session_not_found_returns_404(client: TestClient) -> None:
    token = _register_login(client, "chunk_404@example.com", "chunk_404")
    chunk_data = b"ghost"
    chunk_hash = _sha256(chunk_data)
    resp = client.post(
        "/v1/upload/chunk",
        data={"upload_id": "ghost-session", "chunk_index": "0", "chunk_hash": chunk_hash},
        files={"chunk_file": ("ghost.bin", io.BytesIO(chunk_data), "application/octet-stream")},
        headers=_auth(token),
    )
    assert resp.status_code == 404


# ===========================================================================
# I-C-10 — POST /v1/upload/chunk — Hash mismatch → 400
# ===========================================================================
def test_upload_chunk_hash_mismatch_returns_400(client: TestClient, fr: FakeRedis) -> None:
    """Submitting a wrong chunk_hash must return 400."""
    token = _register_login(client, "chunk_hash@example.com", "chunk_hash")
    ds_id = _create_dataset(client, token, "Hash DS")

    init_resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id, "filename": "hash.csv",
        "filesize": CHUNK_SIZE_BYTES, "master_hash": "g" * 64,
    }, headers=_auth(token))
    upload_id = init_resp.json()["upload_id"]

    chunk_data = b"actual data"
    wrong_hash = "0" * 64  # deliberately wrong

    resp = client.post(
        "/v1/upload/chunk",
        data={"upload_id": upload_id, "chunk_index": "0", "chunk_hash": wrong_hash},
        files={"chunk_file": ("hash.bin", io.BytesIO(chunk_data), "application/octet-stream")},
        headers=_auth(token),
    )
    assert resp.status_code == 400


# ===========================================================================
# I-C-11 — POST /v1/upload/chunk — Missing authorization → 401
# ===========================================================================
def test_upload_chunk_missing_auth_returns_401(client: TestClient) -> None:
    chunk_data = b"data"
    resp = client.post(
        "/v1/upload/chunk",
        data={"upload_id": "some-id", "chunk_index": "0", "chunk_hash": "a" * 64},
        files={"chunk_file": ("f.bin", io.BytesIO(chunk_data), "application/octet-stream")},
    )
    assert resp.status_code == 401


# ===========================================================================
# I-C-12 — POST /v1/upload/finalize — Fast-link path (duplicate_suspected)
# ===========================================================================
def test_upload_finalize_fast_link_path(client: TestClient, db_session: Session, fr: FakeRedis) -> None:
    """Finalizing a duplicate_suspected session must create a mapping, no file assembly."""
    token = _register_login(client, "fin_fast@example.com", "fin_fast")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]
    ds_id = _create_dataset(client, token, "Fin Fast DS")

    # Seed a global file
    gf = UploadedFile(id="fin-fast-file", filename="fin.csv", file_size_bytes=256, master_hash="h" * 64, physical_path="/dev/null")
    db_session.add(gf)
    db_session.commit()

    # init → duplicate_suspected
    init_resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id, "filename": "fin.csv",
        "filesize": 256, "master_hash": "h" * 64,
    }, headers=_auth(token))
    assert init_resp.json()["status"] == "duplicate_suspected"
    upload_id = init_resp.json()["upload_id"]

    # finalize
    fin_resp = client.post("/v1/upload/finalize", json={
        "upload_id": upload_id, "master_hash": "h" * 64
    }, headers=_auth(token))
    assert fin_resp.status_code == 200
    body = fin_resp.json()
    assert body["status"] == "completed"
    assert body["file_id"] == "fin-fast-file"


# ===========================================================================
# I-C-13 — POST /v1/upload/finalize — Standard path (new file assembly)
# ===========================================================================
def test_upload_finalize_standard_new_file(client: TestClient, tmp_path: Path, fr: FakeRedis, monkeypatch) -> None:
    """Full upload flow: init → chunk → finalize assembles the file correctly."""
    token = _register_login(client, "fin_std@example.com", "fin_std")
    ds_id = _create_dataset(client, token, "Std Fin DS")

    file_data = b"Hello, World! This is a test file."
    master_hash = _sha256(file_data)

    # init
    init_resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id, "filename": "std.txt",
        "filesize": len(file_data), "master_hash": master_hash,
    }, headers=_auth(token))
    assert init_resp.json()["status"] == "created"
    upload_id = init_resp.json()["upload_id"]
    total_chunks = init_resp.json()["total_chunks"]
    assert total_chunks == 1

    # chunk
    chunk_hash = _sha256(file_data)
    chunk_resp = client.post(
        "/v1/upload/chunk",
        data={"upload_id": upload_id, "chunk_index": "0", "chunk_hash": chunk_hash},
        files={"chunk_file": ("std.txt", io.BytesIO(file_data), "application/octet-stream")},
        headers=_auth(token),
    )
    assert chunk_resp.status_code == 200
    assert chunk_resp.json()["complete"] is True

    # finalize
    fin_resp = client.post("/v1/upload/finalize", json={
        "upload_id": upload_id, "master_hash": master_hash,
    }, headers=_auth(token))
    assert fin_resp.status_code == 200
    body = fin_resp.json()
    assert body["status"] == "completed"
    assert body["file_id"] is not None


# ===========================================================================
# I-C-14 — POST /v1/upload/finalize — Session not found → 404
# ===========================================================================
def test_upload_finalize_session_not_found_returns_404(client: TestClient) -> None:
    token = _register_login(client, "fin_404@example.com", "fin_404")
    resp = client.post("/v1/upload/finalize", json={"upload_id": "ghost", "master_hash": "a" * 64}, headers=_auth(token))
    assert resp.status_code == 404


# ===========================================================================
# I-C-15 — POST /v1/upload/finalize — master_hash mismatch → 400
# ===========================================================================
def test_upload_finalize_master_hash_mismatch_returns_400(client: TestClient, db_session: Session, fr: FakeRedis) -> None:
    token = _register_login(client, "fin_mismatch@example.com", "fin_mismatch")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]
    ds_id = _create_dataset(client, token, "Mismatch DS")

    gf = UploadedFile(id="mm-file", filename="mm.csv", file_size_bytes=100, master_hash="i" * 64, physical_path="/dev/null")
    db_session.add(gf)
    db_session.commit()

    init_resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id, "filename": "mm.csv", "filesize": 100, "master_hash": "i" * 64,
    }, headers=_auth(token))
    upload_id = init_resp.json()["upload_id"]

    resp = client.post("/v1/upload/finalize", json={
        "upload_id": upload_id, "master_hash": "z" * 64  # wrong
    }, headers=_auth(token))
    assert resp.status_code == 400


# ===========================================================================
# I-C-16 — POST /v1/upload/finalize — Incomplete (0/2 chunks received) → 409
# ===========================================================================
def test_upload_finalize_incomplete_returns_409(client: TestClient, fr: FakeRedis) -> None:
    """Finalizing before all chunks are received must return 409."""
    token = _register_login(client, "fin_incomplete@example.com", "fin_incomplete")
    ds_id = _create_dataset(client, token, "Incomplete DS")

    init_resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id, "filename": "partial.csv",
        "filesize": CHUNK_SIZE_BYTES * 2, "master_hash": "j" * 64,
    }, headers=_auth(token))
    upload_id = init_resp.json()["upload_id"]
    assert init_resp.json()["total_chunks"] == 2
    # Do NOT upload any chunks — finalize immediately

    resp = client.post("/v1/upload/finalize", json={
        "upload_id": upload_id, "master_hash": "j" * 64,
    }, headers=_auth(token))
    assert resp.status_code == 409


# ===========================================================================
# I-C-17 — POST /v1/upload/finalize — Missing authorization → 401
# ===========================================================================
def test_upload_finalize_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.post("/v1/upload/finalize", json={"upload_id": "x", "master_hash": "a" * 64})
    assert resp.status_code == 401


# ===========================================================================
# I-C-18 — GET /v1/uploads — Returns tree for authenticated user
# ===========================================================================
def test_get_uploads_returns_tree(client: TestClient, db_session: Session) -> None:
    """GET /v1/uploads must return a well-formed UploadsTreeResponse."""
    token = _register_login(client, "uploads_tree@example.com", "uploads_tree")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]
    ds_id = _create_dataset(client, token, "Tree Upload DS")

    uf = UploadedFile(id="tree-file", filename="tree.csv", file_size_bytes=64, master_hash="k" * 64, physical_path="/dev/null")
    db_session.add(uf)
    db_session.flush()
    db_session.add(DatasetFolderFilesMapping(dataset_id=ds_id, folder_id=None, file_id=uf.id, user_id=user_id))
    db_session.commit()

    resp = client.get("/v1/uploads", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "folder"
    assert body["name"] == "root"
    file_names = [c["name"] for c in (body.get("children") or []) if c["type"] == "file"]
    assert "tree.csv" in file_names


# ===========================================================================
# I-C-19 — GET /v1/uploads — Filter by dataset_id
# ===========================================================================
def test_get_uploads_filter_by_dataset(client: TestClient, db_session: Session) -> None:
    """?dataset_id= filter returns only files in that dataset."""
    token = _register_login(client, "uploads_filter@example.com", "uploads_filter")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]
    ds1_id = _create_dataset(client, token, "Filter DS1")
    ds2_id = _create_dataset(client, token, "Filter DS2")

    uf1 = UploadedFile(id="filter-f1", filename="file1.csv", file_size_bytes=64, master_hash="l" * 64, physical_path="/dev/null")
    uf2 = UploadedFile(id="filter-f2", filename="file2.csv", file_size_bytes=64, master_hash="m" * 64, physical_path="/dev/null")
    db_session.add_all([uf1, uf2])
    db_session.flush()
    db_session.add(DatasetFolderFilesMapping(dataset_id=ds1_id, folder_id=None, file_id=uf1.id, user_id=user_id))
    db_session.add(DatasetFolderFilesMapping(dataset_id=ds2_id, folder_id=None, file_id=uf2.id, user_id=user_id))
    db_session.commit()

    resp = client.get(f"/v1/uploads?dataset_id={ds1_id}", headers=_auth(token))
    assert resp.status_code == 200
    file_names = [c["name"] for c in (resp.json().get("children") or []) if c["type"] == "file"]
    assert "file1.csv" in file_names
    assert "file2.csv" not in file_names


# ===========================================================================
# I-C-20 — GET /v1/uploads — Missing authorization → 401
# ===========================================================================
def test_get_uploads_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.get("/v1/uploads")
    assert resp.status_code == 401


# ===========================================================================
# I-C-21 — POST /v1/uploads/delete — Happy path
# ===========================================================================
def test_delete_upload_happy_path(client: TestClient, db_session: Session, tmp_path: Path) -> None:
    """Deleting an owned upload must return {status: 'deleted'} and remove the DB row."""
    token = _register_login(client, "del_upload@example.com", "del_upload")
    user_id = client.get("/auth/me", headers=_auth(token)).json()["id"]
    ds_id = _create_dataset(client, token, "Del Upload DS")

    # Create a real temp file
    phys = tmp_path / "files" / "to_delete.csv"
    phys.parent.mkdir(parents=True, exist_ok=True)
    phys.write_bytes(b"delete me")

    uf = UploadedFile(id="del-file", filename="to_delete.csv", file_size_bytes=9, master_hash="n" * 64, physical_path=str(phys))
    db_session.add(uf)
    db_session.flush()
    db_session.add(DatasetFolderFilesMapping(dataset_id=ds_id, folder_id=None, file_id=uf.id, user_id=user_id))
    db_session.commit()

    resp = client.post("/v1/uploads/delete", json={"upload_id": "del-file"}, headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["file_id"] == "del-file"


# ===========================================================================
# I-C-22 — POST /v1/uploads/delete — File not found → 404
# ===========================================================================
def test_delete_upload_not_found_returns_404(client: TestClient) -> None:
    token = _register_login(client, "del_404@example.com", "del_404")
    resp = client.post("/v1/uploads/delete", json={"upload_id": "nonexistent-file-abc"}, headers=_auth(token))
    assert resp.status_code == 404


# ===========================================================================
# I-C-23 — POST /v1/uploads/delete — No ownership mapping → 404
# ===========================================================================
def test_delete_upload_no_ownership_returns_404(client: TestClient, db_session: Session) -> None:
    """File exists but belongs to another user → 404 from ownership check."""
    token_a = _register_login(client, "del_own_a@example.com", "del_own_a")
    token_b = _register_login(client, "del_own_b@example.com", "del_own_b")
    user_a_id = client.get("/auth/me", headers=_auth(token_a)).json()["id"]
    user_b_id = client.get("/auth/me", headers=_auth(token_b)).json()["id"]

    ds_a_id = _create_dataset(client, token_a, "Del Own A DS")
    ds_b_id = _create_dataset(client, token_b, "Del Own B DS")

    # File belongs only to user_b
    uf = UploadedFile(id="del-own-file", filename="owned.csv", file_size_bytes=50, master_hash="o" * 64, physical_path="/dev/null")
    db_session.add(uf)
    db_session.flush()
    db_session.add(DatasetFolderFilesMapping(dataset_id=ds_b_id, folder_id=None, file_id=uf.id, user_id=user_b_id))
    db_session.commit()

    # user_a tries to delete
    resp = client.post("/v1/uploads/delete", json={"upload_id": "del-own-file"}, headers=_auth(token_a))
    assert resp.status_code == 404


# ===========================================================================
# I-C-24 — POST /v1/uploads/delete — Missing authorization → 401
# ===========================================================================
def test_delete_upload_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.post("/v1/uploads/delete", json={"upload_id": "some-file"})
    assert resp.status_code == 401


# ===========================================================================
# I-C-25 — Full upload round-trip: init → chunk → finalize → appears in uploads tree
# ===========================================================================
def test_upload_full_round_trip_visible_in_tree(client: TestClient, fr: FakeRedis) -> None:
    """End-to-end: upload a file and verify it appears in GET /v1/uploads."""
    token = _register_login(client, "round_trip@example.com", "round_trip")
    ds_id = _create_dataset(client, token, "Round Trip DS")

    file_data = b"Round trip test data - module 3"
    master_hash = _sha256(file_data)

    # init
    init_resp = client.post("/v1/upload/init", json={
        "dataset_id": ds_id, "filename": "round.txt",
        "filesize": len(file_data), "master_hash": master_hash,
    }, headers=_auth(token))
    assert init_resp.status_code == 200
    upload_id = init_resp.json()["upload_id"]

    # chunk
    client.post(
        "/v1/upload/chunk",
        data={"upload_id": upload_id, "chunk_index": "0", "chunk_hash": _sha256(file_data)},
        files={"chunk_file": ("round.txt", io.BytesIO(file_data), "application/octet-stream")},
        headers=_auth(token),
    )

    # finalize
    fin = client.post("/v1/upload/finalize", json={"upload_id": upload_id, "master_hash": master_hash}, headers=_auth(token))
    assert fin.status_code == 200

    # verify in tree
    tree_resp = client.get(f"/v1/uploads?dataset_id={ds_id}", headers=_auth(token))
    assert tree_resp.status_code == 200
    file_names = [c["name"] for c in (tree_resp.json().get("children") or []) if c["type"] == "file"]
    assert "round.txt" in file_names
