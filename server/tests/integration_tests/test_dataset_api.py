"""
Module 2 — Integration Tests: Dataset & Virtual Folder Management
=================================================================
Tests all HTTP endpoints for /v1/datasets/* and /v1/datasets/{id}/files
using FastAPI's synchronous TestClient with:
  - In-memory SQLite DB via dependency_overrides
  - FakeRedis stub patched on auth_services + file_services
  - No real PostgreSQL, Redis, or file I/O (file rows are seeded directly)

Known limitation: finalize_upload uses PostgreSQL-specific pg_insert ON CONFLICT DO NOTHING.
The attach_file_to_dataset (zero-I/O link) path is tested by seeding
DatasetFolderFilesMapping rows directly to bypass that limitation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Set required env vars BEFORE importing any application module
os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret-m2-int")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret-m2-int")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("MAX_ACTIVE_SESSIONS", "3")
os.environ.setdefault("UPLOAD_STORAGE_DIR", str(SERVER_ROOT / "uploads"))

from config.database import get_db  # noqa: E402
from models.auth_model import Base as AuthBase, User  # noqa: E402
from models.file_model import (  # noqa: E402
    Base as FileBase,
    Dataset,
    DatasetFolderFilesMapping,
    Folder,
    UploadedFile,
)
from services import auth_services  # noqa: E402
from services import file_services  # noqa: E402
from services.file_services import create_dataset  # noqa: E402
from schemas.file_schema import DatasetCreate  # noqa: E402
from helpers import jwt as jwt_helpers  # noqa: E402
from main import app  # noqa: E402

DEV_OTP = "123456"


# ===========================================================================
# FakeRedis (same pattern as Module 1)
# ===========================================================================

class FakeRedis:
    def __init__(self) -> None:
        self._zsets: dict[str, dict[str, float]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._strings: dict[str, str] = {}

    def zadd(self, key, mapping): self._zsets.setdefault(key, {}).update(mapping)
    def zscore(self, key, member): return self._zsets.get(key, {}).get(member)
    def zcard(self, key): return len(self._zsets.get(key, {}))
    def zrem(self, key, *members):
        zset = self._zsets.get(key, {})
        for m in members: zset.pop(m, None)
    def zremrangebyscore(self, key, mn, mx):
        zset = self._zsets.get(key, {})
        for m in [m for m, s in list(zset.items()) if mn <= s <= mx]: del zset[m]
    def zremrangebyrank(self, key, start, stop):
        zset = self._zsets.get(key, {})
        srt = sorted(zset.items(), key=lambda x: x[1])
        if stop < 0: stop = len(srt) + stop
        for m, _ in srt[start:stop+1]: del zset[m]
    def delete(self, *keys):
        for k in keys:
            self._zsets.pop(k, None); self._hashes.pop(k, None); self._strings.pop(k, None)
    def expire(self, key, seconds): pass
    def hset(self, key, mapping=None, **kw):
        self._hashes.setdefault(key, {}).update(mapping or {}); self._hashes[key].update(kw)
    def hgetall(self, key): return dict(self._hashes.get(key, {}))
    def get(self, key): return self._strings.get(key)
    def set(self, key, value, **kw): self._strings[key] = str(value)
    def bitcount(self, key): return 0
    def getbit(self, key, offset): return 0
    def setbit(self, key, offset, value): pass


def make_fake_limiter(fake_redis: FakeRedis):
    def _limiter(keys, args):
        key = keys[0]; now = int(args[0]); sid = args[1]
        max_sessions = int(args[2]); ttl = int(args[3])
        fake_redis.zremrangebyscore(key, float("-inf"), now - ttl)
        fake_redis.zadd(key, {sid: float(now)})
        card = fake_redis.zcard(key)
        if card > max_sessions:
            fake_redis.zremrangebyrank(key, 0, card - max_sessions - 1)
        return 1
    return _limiter


def make_fake_chunk_state(fake_redis: FakeRedis):
    """Fake atomic_chunk_state Lua script."""
    def _chunk_state(keys, args):
        return 0  # always "new chunk"
    return _chunk_state


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
def db_session(_session_factory) -> Generator[Session, None, None]:
    connection = _session_factory().bind.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def fake_redis_instance() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def client(
    db_session: Session,
    fake_redis_instance: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setattr(auth_services, "redis_server", fake_redis_instance)
    monkeypatch.setattr(auth_services, "active_session_limiter", make_fake_limiter(fake_redis_instance))
    monkeypatch.setattr(file_services, "redis_server", fake_redis_instance)
    monkeypatch.setattr(file_services, "atomic_chunk_state", make_fake_chunk_state(fake_redis_instance))

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
# Auth helper
# ===========================================================================

def _register_verify_login(client: TestClient, email: str, username: str, password: str = "password123") -> str:
    """Full auth flow — returns Bearer access token."""
    client.post("/auth/signup/init", json={"email": email, "username": username, "password": password})
    client.post("/auth/signup/verify", json={"email": email, "otp_code": DEV_OTP})
    resp = client.post("/auth/login", json={"identifier": email, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return resp.headers.get("authorization", "").removeprefix("Bearer ").strip()


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_uploaded_file(db_session: Session) -> UploadedFile:
    """Seed an UploadedFile row without physical I/O."""
    f = UploadedFile(
        id="seed-file-1",
        filename="seed.csv",
        file_size_bytes=512,
        master_hash="b" * 64,
        physical_path="/dev/null/seed.csv",
    )
    db_session.add(f)
    db_session.commit()
    db_session.refresh(f)
    return f


def _seed_mapping(db_session: Session, user_id: str, dataset_id: str, file_id: str, folder_id: str | None = None) -> DatasetFolderFilesMapping:
    """Directly insert a DatasetFolderFilesMapping (bypasses pg_insert ON CONFLICT DO NOTHING)."""
    mapping = DatasetFolderFilesMapping(
        dataset_id=dataset_id,
        folder_id=folder_id,
        file_id=file_id,
        user_id=user_id,
    )
    db_session.add(mapping)
    db_session.commit()
    return mapping


# ===========================================================================
# I-B-01 — POST /v1/datasets — Happy path → 201
# ===========================================================================
def test_create_dataset_happy_path(client: TestClient) -> None:
    """POST /v1/datasets must return 201 with DatasetResponse shape."""
    token = _register_verify_login(client, "ds_create@example.com", "ds_create")
    resp = client.post(
        "/v1/datasets",
        json={"name": "My Dataset", "description": "Test desc", "language": "English"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "My Dataset"
    assert body["status"] == "Created"
    assert body["file_count"] == 0
    assert "id" in body


# ===========================================================================
# I-B-02 — POST /v1/datasets — Missing required fields → 422
# ===========================================================================
def test_create_dataset_missing_fields_returns_422(client: TestClient) -> None:
    """POST /v1/datasets without 'language' must return 422."""
    token = _register_verify_login(client, "ds_422@example.com", "ds_422")
    resp = client.post(
        "/v1/datasets",
        json={"name": "No Language"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 422


# ===========================================================================
# I-B-03 — POST /v1/datasets — Duplicate name → 409
# ===========================================================================
def test_create_dataset_duplicate_name_returns_409(client: TestClient) -> None:
    """Creating a second dataset with the same name must return 409."""
    token = _register_verify_login(client, "ds_dup@example.com", "ds_dup")
    payload = {"name": "Dup DS", "language": "English"}
    first = client.post("/v1/datasets", json=payload, headers=_auth_header(token))
    assert first.status_code == 201
    second = client.post("/v1/datasets", json=payload, headers=_auth_header(token))
    assert second.status_code == 409


# ===========================================================================
# I-B-04 — POST /v1/datasets — Missing Authorization → 401
# ===========================================================================
def test_create_dataset_missing_auth_returns_401(client: TestClient) -> None:
    """POST /v1/datasets without Authorization header must return 401."""
    resp = client.post("/v1/datasets", json={"name": "NoAuth DS", "language": "English"})
    assert resp.status_code == 401


# ===========================================================================
# I-B-05 — GET /v1/datasets — Returns list for authenticated user
# ===========================================================================
def test_list_datasets_returns_user_datasets(client: TestClient) -> None:
    """GET /v1/datasets must return the user's datasets."""
    token = _register_verify_login(client, "ds_list@example.com", "ds_list")
    client.post("/v1/datasets", json={"name": "Listed DS", "language": "English"}, headers=_auth_header(token))

    resp = client.get("/v1/datasets", headers=_auth_header(token))
    assert resp.status_code == 200
    names = [d["name"] for d in resp.json()]
    assert "Listed DS" in names


# ===========================================================================
# I-B-06 — GET /v1/datasets — Missing Authorization → 401
# ===========================================================================
def test_list_datasets_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.get("/v1/datasets")
    assert resp.status_code == 401


# ===========================================================================
# I-B-07 — GET /v1/datasets — Pagination params respected
# ===========================================================================
def test_list_datasets_pagination(client: TestClient) -> None:
    """Pagination query params must correctly limit returned results."""
    token = _register_verify_login(client, "ds_page@example.com", "ds_page")
    for i in range(4):
        client.post("/v1/datasets", json={"name": f"Page-DS-{i}", "language": "English"}, headers=_auth_header(token))

    resp = client.get("/v1/datasets?page=1&limit=2", headers=_auth_header(token))
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ===========================================================================
# I-B-08 — GET /v1/datasets/{id} — Happy path → 200 + DatasetResponse
# ===========================================================================
def test_get_dataset_by_id_happy_path(client: TestClient) -> None:
    """GET /v1/datasets/{id} must return 200 with dataset details."""
    token = _register_verify_login(client, "ds_get@example.com", "ds_get")
    create_resp = client.post(
        "/v1/datasets",
        json={"name": "Fetch DS", "language": "English"},
        headers=_auth_header(token),
    )
    ds_id = create_resp.json()["id"]

    resp = client.get(f"/v1/datasets/{ds_id}", headers=_auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["id"] == ds_id
    assert "tree" in resp.json()


# ===========================================================================
# I-B-09 — GET /v1/datasets/{id} — Non-existent ID → 404
# ===========================================================================
def test_get_dataset_by_id_not_found_returns_404(client: TestClient) -> None:
    token = _register_verify_login(client, "ds_404@example.com", "ds_404")
    resp = client.get("/v1/datasets/nonexistent-id-999", headers=_auth_header(token))
    assert resp.status_code == 404


# ===========================================================================
# I-B-10 — GET /v1/datasets/{id} — Another user's dataset → 403
# ===========================================================================
def test_get_dataset_by_id_wrong_user_returns_403(client: TestClient) -> None:
    """Fetching another user's dataset must return 403 Forbidden."""
    token_a = _register_verify_login(client, "ds_a@example.com", "ds_a")
    token_b = _register_verify_login(client, "ds_b@example.com", "ds_b")

    create_resp = client.post(
        "/v1/datasets",
        json={"name": "A's Dataset", "language": "English"},
        headers=_auth_header(token_a),
    )
    ds_id = create_resp.json()["id"]

    resp = client.get(f"/v1/datasets/{ds_id}", headers=_auth_header(token_b))
    assert resp.status_code == 403


# ===========================================================================
# I-B-11 — PATCH /v1/datasets/{id} — Rename happy path → 200
# ===========================================================================
def test_patch_dataset_rename_happy_path(client: TestClient) -> None:
    token = _register_verify_login(client, "ds_patch@example.com", "ds_patch")
    ds_id = client.post(
        "/v1/datasets", json={"name": "Original", "language": "English"}, headers=_auth_header(token)
    ).json()["id"]

    resp = client.patch(
        f"/v1/datasets/{ds_id}", json={"name": "Renamed"}, headers=_auth_header(token)
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"


# ===========================================================================
# I-B-12 — PATCH /v1/datasets/{id} — Duplicate name → 409
# ===========================================================================
def test_patch_dataset_duplicate_name_returns_409(client: TestClient) -> None:
    token = _register_verify_login(client, "ds_pdup@example.com", "ds_pdup")
    client.post("/v1/datasets", json={"name": "Existing", "language": "English"}, headers=_auth_header(token))
    ds_id = client.post("/v1/datasets", json={"name": "Target", "language": "English"}, headers=_auth_header(token)).json()["id"]

    resp = client.patch(f"/v1/datasets/{ds_id}", json={"name": "Existing"}, headers=_auth_header(token))
    assert resp.status_code == 409


# ===========================================================================
# I-B-13 — PATCH /v1/datasets/{id} — Status transition Created → Completed
# ===========================================================================
def test_patch_dataset_status_transition_to_completed(client: TestClient) -> None:
    token = _register_verify_login(client, "ds_complete@example.com", "ds_complete")
    ds_id = client.post("/v1/datasets", json={"name": "To Complete", "language": "English"}, headers=_auth_header(token)).json()["id"]

    resp = client.patch(f"/v1/datasets/{ds_id}", json={"status": "Completed"}, headers=_auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "Completed"


# ===========================================================================
# I-B-14 — PATCH /v1/datasets/{id} — Invalid status transition → 400
# ===========================================================================
def test_patch_dataset_invalid_status_transition_returns_400(client: TestClient) -> None:
    token = _register_verify_login(client, "ds_badst@example.com", "ds_badst")
    ds_id = client.post("/v1/datasets", json={"name": "DS Bad Status", "language": "English"}, headers=_auth_header(token)).json()["id"]
    # Complete the dataset first
    client.patch(f"/v1/datasets/{ds_id}", json={"status": "Completed"}, headers=_auth_header(token))
    # Now try to transition from Completed → Draft
    resp = client.patch(f"/v1/datasets/{ds_id}", json={"status": "Draft"}, headers=_auth_header(token))
    assert resp.status_code == 400


# ===========================================================================
# I-B-15 — PATCH /v1/datasets/{id} — Missing Authorization → 401
# ===========================================================================
def test_patch_dataset_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.patch("/v1/datasets/some-id", json={"name": "X"})
    assert resp.status_code == 401


# ===========================================================================
# I-B-16 — DELETE /v1/datasets/{id} — Happy path → 200
# ===========================================================================
def test_delete_dataset_happy_path(client: TestClient) -> None:
    token = _register_verify_login(client, "ds_del@example.com", "ds_del")
    ds_id = client.post("/v1/datasets", json={"name": "To Delete", "language": "English"}, headers=_auth_header(token)).json()["id"]

    resp = client.delete(f"/v1/datasets/{ds_id}", headers=_auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["dataset_id"] == ds_id


# ===========================================================================
# I-B-17 — DELETE /v1/datasets/{id} — Non-existent → 404
# ===========================================================================
def test_delete_dataset_not_found_returns_404(client: TestClient) -> None:
    token = _register_verify_login(client, "ds_del404@example.com", "ds_del404")
    resp = client.delete("/v1/datasets/nonexistent-del-999", headers=_auth_header(token))
    assert resp.status_code == 404


# ===========================================================================
# I-B-18 — DELETE /v1/datasets/{id} — Wrong owner → 403
# ===========================================================================
def test_delete_dataset_wrong_owner_returns_403(client: TestClient) -> None:
    token_a = _register_verify_login(client, "ds_dela@example.com", "ds_dela")
    token_b = _register_verify_login(client, "ds_delb@example.com", "ds_delb")

    ds_id = client.post("/v1/datasets", json={"name": "A Owns This", "language": "English"}, headers=_auth_header(token_a)).json()["id"]
    resp = client.delete(f"/v1/datasets/{ds_id}", headers=_auth_header(token_b))
    assert resp.status_code == 403


# ===========================================================================
# I-B-19 — DELETE /v1/datasets/{id} — Missing Authorization → 401
# ===========================================================================
def test_delete_dataset_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.delete("/v1/datasets/some-id")
    assert resp.status_code == 401


# ===========================================================================
# I-B-20 — POST /v1/datasets/{id}/files — File not found → 404
# ===========================================================================
def test_attach_file_not_found_returns_404(client: TestClient) -> None:
    """Attaching a non-existent file_id must return 404."""
    token = _register_verify_login(client, "ds_att404@example.com", "ds_att404")
    ds_id = client.post("/v1/datasets", json={"name": "Attach DS 404", "language": "English"}, headers=_auth_header(token)).json()["id"]

    resp = client.post(
        f"/v1/datasets/{ds_id}/files",
        json={"file_id": "nonexistent-file-xyz"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 404


# ===========================================================================
# I-B-21 — POST /v1/datasets/{id}/files — IDOR: file exists but belongs to other user → 403
# ===========================================================================
def test_attach_file_idor_returns_403(client: TestClient, db_session: Session) -> None:
    """Attempting to attach another user's file must return 403."""
    token_a = _register_verify_login(client, "ds_idor_a@example.com", "ds_idor_a")
    token_b = _register_verify_login(client, "ds_idor_b@example.com", "ds_idor_b")

    # Get user IDs from /auth/me
    user_b_id = client.get("/auth/me", headers=_auth_header(token_b)).json()["id"]
    user_a_id = client.get("/auth/me", headers=_auth_header(token_a)).json()["id"]

    # Create dataset for A and B
    ds_a_id = client.post("/v1/datasets", json={"name": "A DS IDOR", "language": "English"}, headers=_auth_header(token_a)).json()["id"]
    ds_b_id = client.post("/v1/datasets", json={"name": "B DS IDOR", "language": "English"}, headers=_auth_header(token_b)).json()["id"]

    # Seed an UploadedFile and assign it ONLY to user B
    uf = UploadedFile(id="idor-file-99", filename="idor.csv", file_size_bytes=100, master_hash="c" * 64, physical_path="/dev/null")
    db_session.add(uf)
    db_session.commit()
    _seed_mapping(db_session, user_b_id, ds_b_id, "idor-file-99")

    # User A tries to attach user B's file
    resp = client.post(
        f"/v1/datasets/{ds_a_id}/files",
        json={"file_id": "idor-file-99"},
        headers=_auth_header(token_a),
    )
    assert resp.status_code == 403


# ===========================================================================
# I-B-22 — POST /v1/datasets/{id}/files — Happy path (zero-I/O attach)
# ===========================================================================
def test_attach_file_happy_path(client: TestClient, db_session: Session) -> None:
    """User can attach their own file to a new dataset via zero-I/O link."""
    token = _register_verify_login(client, "ds_attach_ok@example.com", "ds_attach_ok")
    user_id = client.get("/auth/me", headers=_auth_header(token)).json()["id"]

    ds1_id = client.post("/v1/datasets", json={"name": "Source DS", "language": "English"}, headers=_auth_header(token)).json()["id"]
    ds2_id = client.post("/v1/datasets", json={"name": "Target DS", "language": "English"}, headers=_auth_header(token)).json()["id"]

    # Seed file + mapping under user (owned by user via ds1)
    uf = UploadedFile(id="attach-ok-file", filename="ok.csv", file_size_bytes=256, master_hash="d" * 64, physical_path="/dev/null")
    db_session.add(uf)
    db_session.commit()
    _seed_mapping(db_session, user_id, ds1_id, "attach-ok-file")

    resp = client.post(
        f"/v1/datasets/{ds2_id}/files",
        json={"file_id": "attach-ok-file"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "attached"
    assert body["file_id"] == "attach-ok-file"
    assert body["dataset_id"] == ds2_id


# ===========================================================================
# I-B-23 — POST /v1/datasets/{id}/files — Attach to Completed dataset → 400
# ===========================================================================
def test_attach_file_to_completed_dataset_returns_400(client: TestClient, db_session: Session) -> None:
    token = _register_verify_login(client, "ds_att_comp@example.com", "ds_att_comp")
    user_id = client.get("/auth/me", headers=_auth_header(token)).json()["id"]

    ds1_id = client.post("/v1/datasets", json={"name": "Source Comp", "language": "English"}, headers=_auth_header(token)).json()["id"]
    ds_comp_id = client.post("/v1/datasets", json={"name": "Comp DS", "language": "English"}, headers=_auth_header(token)).json()["id"]

    # Complete the target dataset
    client.patch(f"/v1/datasets/{ds_comp_id}", json={"status": "Completed"}, headers=_auth_header(token))

    # Seed file
    uf = UploadedFile(id="comp-att-file", filename="comp.csv", file_size_bytes=128, master_hash="e" * 64, physical_path="/dev/null")
    db_session.add(uf)
    db_session.commit()
    _seed_mapping(db_session, user_id, ds1_id, "comp-att-file")

    resp = client.post(
        f"/v1/datasets/{ds_comp_id}/files",
        json={"file_id": "comp-att-file"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 400


# ===========================================================================
# I-B-24 — POST /v1/datasets/{id}/files — Missing Authorization → 401
# ===========================================================================
def test_attach_file_missing_auth_returns_401(client: TestClient) -> None:
    resp = client.post("/v1/datasets/some-id/files", json={"file_id": "f1"})
    assert resp.status_code == 401


# ===========================================================================
# I-B-25 — GET /v1/datasets/{id} — Tree is populated after attach
# ===========================================================================
def test_get_dataset_tree_shows_attached_files(client: TestClient, db_session: Session) -> None:
    """After attaching a file, GET /v1/datasets/{id} must include it in the tree."""
    token = _register_verify_login(client, "ds_tree@example.com", "ds_tree")
    user_id = client.get("/auth/me", headers=_auth_header(token)).json()["id"]

    ds_id = client.post("/v1/datasets", json={"name": "Tree DS", "language": "English"}, headers=_auth_header(token)).json()["id"]

    uf = UploadedFile(id="tree-file-1", filename="tree.csv", file_size_bytes=64, master_hash="f" * 64, physical_path="/dev/null")
    db_session.add(uf)
    db_session.commit()
    _seed_mapping(db_session, user_id, ds_id, "tree-file-1")

    resp = client.get(f"/v1/datasets/{ds_id}", headers=_auth_header(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["file_count"] == 1
    tree = body["tree"]
    assert tree is not None
    # Flatten children to find the file
    all_children = tree.get("children") or []
    file_names = [c["name"] for c in all_children if c["type"] == "file"]
    assert "tree.csv" in file_names
