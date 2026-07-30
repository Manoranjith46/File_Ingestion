"""
Module 2 — Unit Tests: Dataset & Virtual Folder Management
===========================================================
Covers: create_dataset, get_datasets, get_dataset_by_id, update_dataset,
        delete_dataset, attach_file_to_dataset, _get_folder_tree,
        _resolve_folder_parts, _validate_relative_path, _sync_dataset_status_from_mappings,
        _normalize_dataset_status, get_user_integrations.

Isolation strategy:
  - In-memory SQLite via SQLAlchemy (created fresh per test).
  - FakeRedis stub replaces real Redis (auth_services only; file_services Redis
    is not exercised in dataset/folder unit tests).
  - pg_insert ON CONFLICT DO NOTHING is NOT used in dataset tests (only in finalize_upload).
  - No network I/O, no real PostgreSQL, no file I/O.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator

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

# Required env vars before any app import
os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret-m2")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret-m2")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("MAX_ACTIVE_SESSIONS", "3")
os.environ.setdefault("UPLOAD_STORAGE_DIR", str(SERVER_ROOT / "uploads"))

from models.auth_model import Base as AuthBase, User  # noqa: E402
from models.file_model import (  # noqa: E402
    Base as FileBase,
    Dataset,
    DatasetFolderFilesMapping,
    Folder,
    UploadedFile,
)
from schemas.file_schema import (  # noqa: E402
    DatasetAttachFileRequest,
    DatasetCreate,
    DatasetUpdate,
)
from services import file_services  # noqa: E402
from services.file_services import (  # noqa: E402
    _normalize_dataset_status,
    _resolve_folder_parts,
    _validate_relative_path,
    attach_file_to_dataset,
    create_dataset,
    delete_dataset,
    get_dataset_by_id,
    get_datasets,
    get_user_integrations,
    update_dataset,
    _get_folder_tree,
    _sync_dataset_status_from_mappings,
)


# ===========================================================================
# Shared Base — combine auth + file metadata
# ===========================================================================
from sqlalchemy import MetaData
COMBINED_META = MetaData()


# ===========================================================================
# Fixtures
# ===========================================================================

def _make_engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Isolated in-memory SQLite session per test — both auth + file tables."""
    engine = _make_engine()
    # Create both bases on the same engine
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
def user(db_session: Session) -> User:
    """Persist and return a verified user."""
    u = User(
        id="user-ds-1",
        email="ds@example.com",
        username="dsuser",
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
    """Second user for ownership isolation tests."""
    u = User(
        id="user-other-1",
        email="other@example.com",
        username="otheruser",
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
    """Create and return a fresh dataset for the test user."""
    return create_dataset(
        db_session,
        user,
        DatasetCreate(name="Test Dataset", description="desc", language="English"),
    )


@pytest.fixture()
def uploaded_file(db_session: Session) -> UploadedFile:
    """Create and return a minimal UploadedFile row (no physical I/O)."""
    f = UploadedFile(
        id="file-1",
        filename="test.csv",
        file_size_bytes=1024,
        master_hash="a" * 64,
        physical_path="/dev/null/test.csv",
    )
    db_session.add(f)
    db_session.commit()
    db_session.refresh(f)
    return f


# ===========================================================================
# HELPERS
# ===========================================================================

def _attach_file_direct(
    db: Session, user: User, dataset: Dataset, file: UploadedFile, folder: Folder | None = None
) -> DatasetFolderFilesMapping:
    """Directly insert a mapping row (bypasses pg_insert ON CONFLICT DO NOTHING)."""
    mapping = DatasetFolderFilesMapping(
        dataset_id=dataset.id,
        folder_id=folder.id if folder else None,
        file_id=file.id,
        user_id=user.id,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


# ===========================================================================
# U-B-01 — create_dataset: happy path
# ===========================================================================
def test_create_dataset_happy_path(db_session: Session, user: User) -> None:
    """New dataset should be persisted with correct fields and status 'Created'."""
    ds = create_dataset(
        db_session,
        user,
        DatasetCreate(name="My Dataset", description="My desc", language="English"),
    )
    assert ds.id is not None
    assert ds.name == "My Dataset"
    assert ds.description == "My desc"
    assert ds.status == "Created"
    assert ds.user_id == user.id
    assert ds.is_deleted is False


# ===========================================================================
# U-B-02 — create_dataset: duplicate name (same user) → 409
# ===========================================================================
def test_create_dataset_duplicate_name_raises_409(db_session: Session, user: User) -> None:
    """Creating two datasets with the same name for the same user must raise 409."""
    create_dataset(db_session, user, DatasetCreate(name="Dup Dataset", language="English"))
    with pytest.raises(HTTPException) as exc:
        create_dataset(db_session, user, DatasetCreate(name="Dup Dataset", language="English"))
    assert exc.value.status_code == 409


# ===========================================================================
# U-B-03 — create_dataset: case-insensitive duplicate detection
# ===========================================================================
def test_create_dataset_case_insensitive_duplicate_raises_409(db_session: Session, user: User) -> None:
    """Dataset names must be unique case-insensitively (e.g. 'test' == 'TEST')."""
    create_dataset(db_session, user, DatasetCreate(name="CaseTest", language="English"))
    with pytest.raises(HTTPException) as exc:
        create_dataset(db_session, user, DatasetCreate(name="casetest", language="English"))
    assert exc.value.status_code == 409


# ===========================================================================
# U-B-04 — create_dataset: different users may share a name
# ===========================================================================
def test_create_dataset_different_users_share_name(
    db_session: Session, user: User, other_user: User
) -> None:
    """Two different users must be allowed to create datasets with the same name."""
    ds1 = create_dataset(db_session, user, DatasetCreate(name="Shared Name", language="English"))
    ds2 = create_dataset(db_session, other_user, DatasetCreate(name="Shared Name", language="English"))
    assert ds1.id != ds2.id


# ===========================================================================
# U-B-05 — get_datasets: returns only non-deleted datasets for user
# ===========================================================================
def test_get_datasets_returns_only_user_datasets(
    db_session: Session, user: User, other_user: User
) -> None:
    """get_datasets must return only datasets belonging to the requesting user."""
    create_dataset(db_session, user, DatasetCreate(name="DS-User", language="English"))
    create_dataset(db_session, other_user, DatasetCreate(name="DS-Other", language="English"))

    results = get_datasets(db_session, user)
    names = [d.name for d in results]
    assert "DS-User" in names
    assert "DS-Other" not in names


# ===========================================================================
# U-B-06 — get_datasets: soft-deleted datasets are excluded
# ===========================================================================
def test_get_datasets_excludes_soft_deleted(db_session: Session, user: User) -> None:
    """Soft-deleted (is_deleted=True) datasets must not appear in get_datasets."""
    ds = create_dataset(db_session, user, DatasetCreate(name="ToDelete", language="English"))
    ds.is_deleted = True
    db_session.commit()

    results = get_datasets(db_session, user)
    assert all(d.id != ds.id for d in results)


# ===========================================================================
# U-B-07 — get_datasets: pagination (page/limit)
# ===========================================================================
def test_get_datasets_pagination(db_session: Session, user: User) -> None:
    """Pagination should correctly limit and offset results."""
    for i in range(5):
        create_dataset(db_session, user, DatasetCreate(name=f"DS-{i}", language="English"))

    page1 = get_datasets(db_session, user, page=1, limit=3)
    page2 = get_datasets(db_session, user, page=2, limit=3)
    assert len(page1) == 3
    assert len(page2) == 2
    # No overlap between pages
    ids1 = {d.id for d in page1}
    ids2 = {d.id for d in page2}
    assert ids1.isdisjoint(ids2)


# ===========================================================================
# U-B-08 — get_datasets: include_completed=False excludes Completed datasets
# ===========================================================================
def test_get_datasets_exclude_completed(db_session: Session, user: User) -> None:
    """When include_completed=False, Completed datasets must not be returned."""
    ds_completed = create_dataset(db_session, user, DatasetCreate(name="DS-C", language="English"))
    ds_completed.status = "Completed"
    db_session.commit()
    ds_draft = create_dataset(db_session, user, DatasetCreate(name="DS-D", language="English"))

    results = get_datasets(db_session, user, include_completed=False)
    ids = [d.id for d in results]
    assert ds_completed.id not in ids
    assert ds_draft.id in ids


# ===========================================================================
# U-B-09 — get_dataset_by_id: happy path
# ===========================================================================
def test_get_dataset_by_id_happy_path(db_session: Session, user: User, dataset: Dataset) -> None:
    """Fetching by valid ID + correct owner should return the dataset."""
    result = get_dataset_by_id(db_session, user, dataset.id)
    assert result.id == dataset.id
    assert result.user_id == user.id


# ===========================================================================
# U-B-10 — get_dataset_by_id: wrong owner → 403
# ===========================================================================
def test_get_dataset_by_id_wrong_owner_raises_403(
    db_session: Session, user: User, other_user: User, dataset: Dataset
) -> None:
    """Fetching another user's dataset must raise HTTP 403."""
    with pytest.raises(HTTPException) as exc:
        get_dataset_by_id(db_session, other_user, dataset.id)
    assert exc.value.status_code == 403


# ===========================================================================
# U-B-11 — get_dataset_by_id: non-existent ID → 404
# ===========================================================================
def test_get_dataset_by_id_not_found_raises_404(db_session: Session, user: User) -> None:
    """Fetching a non-existent dataset ID must raise HTTP 404."""
    with pytest.raises(HTTPException) as exc:
        get_dataset_by_id(db_session, user, "nonexistent-id-xxx")
    assert exc.value.status_code == 404


# ===========================================================================
# U-B-12 — get_dataset_by_id: soft-deleted dataset → 404
# ===========================================================================
def test_get_dataset_by_id_soft_deleted_raises_404(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Soft-deleted datasets must not be retrievable (treated as 404)."""
    dataset.is_deleted = True
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        get_dataset_by_id(db_session, user, dataset.id)
    assert exc.value.status_code == 404


# ===========================================================================
# U-B-13 — update_dataset: rename happy path
# ===========================================================================
def test_update_dataset_rename_happy_path(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Renaming a dataset to a new unique name must persist the change."""
    updated = update_dataset(db_session, user, dataset.id, DatasetUpdate(name="Renamed Dataset"))
    assert updated.name == "Renamed Dataset"


# ===========================================================================
# U-B-14 — update_dataset: rename to same name (no-op) — no 409
# ===========================================================================
def test_update_dataset_rename_to_same_name_is_allowed(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Renaming a dataset to its current name (same case) must not raise 409."""
    updated = update_dataset(db_session, user, dataset.id, DatasetUpdate(name=dataset.name))
    assert updated.name == dataset.name


# ===========================================================================
# U-B-15 — update_dataset: rename collision with other dataset → 409
# ===========================================================================
def test_update_dataset_rename_collision_raises_409(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Renaming a dataset to a name already taken by another dataset must raise 409."""
    other = create_dataset(db_session, user, DatasetCreate(name="Other Dataset", language="English"))
    with pytest.raises(HTTPException) as exc:
        update_dataset(db_session, user, dataset.id, DatasetUpdate(name="Other Dataset"))
    assert exc.value.status_code == 409


# ===========================================================================
# U-B-16 — update_dataset: modify Completed dataset → 400
# ===========================================================================
def test_update_dataset_completed_raises_400(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Updating a Completed dataset (except status transition) must raise 400."""
    dataset.status = "Completed"
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        update_dataset(db_session, user, dataset.id, DatasetUpdate(name="New Name"))
    assert exc.value.status_code == 400


# ===========================================================================
# U-B-17 — update_dataset: status transition Created → Draft (valid)
# ===========================================================================
def test_update_dataset_status_created_to_draft(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Status transition from Created → Draft must be allowed."""
    updated = update_dataset(db_session, user, dataset.id, DatasetUpdate(status="Draft"))
    assert updated.status == "Draft"


# ===========================================================================
# U-B-18 — update_dataset: invalid status transition Draft → Created → 400
# ===========================================================================
def test_update_dataset_invalid_status_transition_raises_400(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Invalid status transition (e.g. Completed → Draft) must raise 400."""
    dataset.status = "Completed"
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        update_dataset(db_session, user, dataset.id, DatasetUpdate(status="Draft"))
    assert exc.value.status_code == 400


# ===========================================================================
# U-B-19 — update_dataset: description update
# ===========================================================================
def test_update_dataset_description(db_session: Session, user: User, dataset: Dataset) -> None:
    """Updating description must persist the new value."""
    updated = update_dataset(
        db_session, user, dataset.id, DatasetUpdate(description="New description")
    )
    assert updated.description == "New description"


# ===========================================================================
# U-B-20 — delete_dataset: hard deletes the row
# ===========================================================================
def test_delete_dataset_removes_record(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """delete_dataset must remove the row from the DB permanently."""
    ds_id = dataset.id
    delete_dataset(db_session, user, ds_id)
    with pytest.raises(HTTPException) as exc:
        get_dataset_by_id(db_session, user, ds_id)
    assert exc.value.status_code == 404


# ===========================================================================
# U-B-21 — delete_dataset: cascades to delete linked file mappings
# ===========================================================================
def test_delete_dataset_cascades_mappings(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile
) -> None:
    """Deleting a dataset must also remove all its DatasetFolderFilesMapping rows."""
    _attach_file_direct(db_session, user, dataset, uploaded_file)
    ds_id = dataset.id

    delete_dataset(db_session, user, ds_id)

    remaining = (
        db_session.query(DatasetFolderFilesMapping)
        .filter(DatasetFolderFilesMapping.dataset_id == ds_id)
        .all()
    )
    assert len(remaining) == 0


# ===========================================================================
# U-B-22 — delete_dataset: wrong owner → 403
# ===========================================================================
def test_delete_dataset_wrong_owner_raises_403(
    db_session: Session, user: User, other_user: User, dataset: Dataset
) -> None:
    """Deleting another user's dataset must raise 403."""
    with pytest.raises(HTTPException) as exc:
        delete_dataset(db_session, other_user, dataset.id)
    assert exc.value.status_code == 403


# ===========================================================================
# U-B-23 — attach_file_to_dataset: happy path (zero-I/O link)
# ===========================================================================
def test_attach_file_to_dataset_happy_path(
    db_session: Session,
    user: User,
    dataset: Dataset,
    uploaded_file: UploadedFile,
) -> None:
    """Attaching an owned file to a dataset must create a mapping row."""
    # First create a self-referencing mapping so IDOR check passes
    _attach_file_direct(db_session, user, dataset, uploaded_file)

    # Create a second dataset to attach into
    ds2 = create_dataset(db_session, user, DatasetCreate(name="DS2", language="English"))
    result = attach_file_to_dataset(
        db_session,
        user,
        ds2.id,
        DatasetAttachFileRequest(file_id=uploaded_file.id),
    )
    assert result.status == "attached"
    assert result.file_id == uploaded_file.id
    assert result.dataset_id == ds2.id


# ===========================================================================
# U-B-24 — attach_file_to_dataset: file not found → 404
# ===========================================================================
def test_attach_file_to_dataset_not_found_raises_404(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """Attaching a non-existent file ID must raise HTTP 404."""
    with pytest.raises(HTTPException) as exc:
        attach_file_to_dataset(
            db_session,
            user,
            dataset.id,
            DatasetAttachFileRequest(file_id="nonexistent-file-xxx"),
        )
    assert exc.value.status_code == 404


# ===========================================================================
# U-B-25 — attach_file_to_dataset: IDOR — file exists but user doesn't own it → 403
# ===========================================================================
def test_attach_file_to_dataset_idor_raises_403(
    db_session: Session,
    user: User,
    other_user: User,
    dataset: Dataset,
    uploaded_file: UploadedFile,
) -> None:
    """Attaching a file that exists but is owned by another user must raise 403."""
    # Create the dataset for other_user and attach the file under them
    other_ds = create_dataset(
        db_session, other_user, DatasetCreate(name="Other DS", language="English")
    )
    _attach_file_direct(db_session, other_user, other_ds, uploaded_file)

    # user attempts to attach the same file — IDOR check must block this
    with pytest.raises(HTTPException) as exc:
        attach_file_to_dataset(
            db_session,
            user,
            dataset.id,
            DatasetAttachFileRequest(file_id=uploaded_file.id),
        )
    assert exc.value.status_code == 403


# ===========================================================================
# U-B-26 — attach_file_to_dataset: completed dataset → 400
# ===========================================================================
def test_attach_file_to_completed_dataset_raises_400(
    db_session: Session,
    user: User,
    dataset: Dataset,
    uploaded_file: UploadedFile,
) -> None:
    """Attaching a file to a Completed dataset must raise 400."""
    _attach_file_direct(db_session, user, dataset, uploaded_file)
    dataset.status = "Completed"
    db_session.commit()

    ds2 = create_dataset(db_session, user, DatasetCreate(name="DS-Completed", language="English"))
    ds2.status = "Completed"
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        attach_file_to_dataset(
            db_session,
            user,
            ds2.id,
            DatasetAttachFileRequest(file_id=uploaded_file.id),
        )
    assert exc.value.status_code == 400


# ===========================================================================
# U-B-27 — _get_folder_tree: creates nested folder hierarchy
# ===========================================================================
def test_get_folder_tree_creates_nested_folders(db_session: Session, user: User) -> None:
    """_get_folder_tree must create and return nested folders for a relative path."""
    folder = _get_folder_tree(db_session, user, "docs/reports/2024", filename=None)
    db_session.commit()

    assert folder is not None
    assert folder.name == "2024"
    assert folder.parent_id is not None

    parent = db_session.query(Folder).filter(Folder.id == folder.parent_id).one()
    assert parent.name == "reports"

    grandparent = db_session.query(Folder).filter(Folder.id == parent.parent_id).one()
    assert grandparent.name == "docs"
    assert grandparent.parent_id is None


# ===========================================================================
# U-B-28 — _get_folder_tree: idempotent — second call returns existing folder
# ===========================================================================
def test_get_folder_tree_idempotent(db_session: Session, user: User) -> None:
    """Calling _get_folder_tree twice with the same path must not duplicate folders."""
    folder1 = _get_folder_tree(db_session, user, "shared/sub", filename=None)
    db_session.commit()
    folder2 = _get_folder_tree(db_session, user, "shared/sub", filename=None)
    db_session.commit()

    assert folder1 is not None
    assert folder2 is not None
    assert folder1.id == folder2.id

    # Only 2 folder rows total: shared + sub
    count = (
        db_session.query(Folder).filter(Folder.user_id == user.id).count()
    )
    assert count == 2


# ===========================================================================
# U-B-29 — _get_folder_tree: None path → returns None (root level)
# ===========================================================================
def test_get_folder_tree_none_path_returns_none(db_session: Session, user: User) -> None:
    """A None relative_path must return None (file goes to root of dataset)."""
    result = _get_folder_tree(db_session, user, None, filename=None)
    assert result is None


# ===========================================================================
# U-B-30 — _validate_relative_path: rejects absolute paths → 400
# ===========================================================================
def test_validate_relative_path_absolute_raises_400() -> None:
    """Absolute paths starting with '/' must raise HTTP 400."""
    with pytest.raises(HTTPException) as exc:
        _validate_relative_path("/etc/passwd")
    assert exc.value.status_code == 400


# ===========================================================================
# U-B-31 — _validate_relative_path: rejects traversal segments → 400
# ===========================================================================
def test_validate_relative_path_traversal_raises_400() -> None:
    """Path segments containing '..' must raise HTTP 400."""
    with pytest.raises(HTTPException) as exc:
        _validate_relative_path("docs/../../../etc/passwd")
    assert exc.value.status_code == 400


# ===========================================================================
# U-B-32 — _validate_relative_path: empty string → empty list
# ===========================================================================
def test_validate_relative_path_empty_returns_empty() -> None:
    """An empty string must return an empty parts list."""
    assert _validate_relative_path("") == []
    assert _validate_relative_path(None) == []


# ===========================================================================
# U-B-33 — _resolve_folder_parts: strips trailing filename segment
# ===========================================================================
def test_resolve_folder_parts_strips_filename() -> None:
    """When the last path segment matches the filename, it must be stripped."""
    parts = _resolve_folder_parts("docs/report.csv", filename="report.csv")
    assert parts == ["docs"]


# ===========================================================================
# U-B-34 — _resolve_folder_parts: preserves all parts when no filename match
# ===========================================================================
def test_resolve_folder_parts_no_strip_when_no_match() -> None:
    """When filename does not match the last segment, all parts are returned."""
    parts = _resolve_folder_parts("docs/sub", filename="other.csv")
    assert parts == ["docs", "sub"]


# ===========================================================================
# U-B-35 — _normalize_dataset_status: valid lifecycle states
# ===========================================================================
@pytest.mark.parametrize("raw,expected", [
    ("created", "Created"),
    ("draft", "Draft"),
    ("completed", "Completed"),
    ("in progress", "Draft"),
    ("unknown", "Created"),
    (None, "Created"),
])
def test_normalize_dataset_status(raw, expected) -> None:
    """All status values must normalize correctly to lifecycle states."""
    assert _normalize_dataset_status(raw) == expected


# ===========================================================================
# U-B-36 — _sync_dataset_status_from_mappings: 0 files → Created
# ===========================================================================
def test_sync_dataset_status_no_files_sets_created(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """A dataset with no files should sync to 'Created' status."""
    dataset.status = "Draft"
    _sync_dataset_status_from_mappings(db_session, dataset)
    assert dataset.status == "Created"


# ===========================================================================
# U-B-37 — _sync_dataset_status_from_mappings: >0 files → Draft
# ===========================================================================
def test_sync_dataset_status_with_files_sets_draft(
    db_session: Session, user: User, dataset: Dataset, uploaded_file: UploadedFile
) -> None:
    """A dataset with at least one file mapping should sync to 'Draft' status."""
    _attach_file_direct(db_session, user, dataset, uploaded_file)
    dataset.status = "Created"
    _sync_dataset_status_from_mappings(db_session, dataset)
    assert dataset.status == "Draft"


# ===========================================================================
# U-B-38 — _sync_dataset_status_from_mappings: Completed is never downgraded
# ===========================================================================
def test_sync_dataset_status_completed_not_downgraded(
    db_session: Session, user: User, dataset: Dataset
) -> None:
    """A Completed dataset must never be changed by _sync_dataset_status_from_mappings."""
    dataset.status = "Completed"
    _sync_dataset_status_from_mappings(db_session, dataset)
    assert dataset.status == "Completed"


# ===========================================================================
# U-B-39 — get_user_integrations: no tokens → both False
# ===========================================================================
def test_get_user_integrations_no_tokens(user: User) -> None:
    """A user with no OAuth tokens must have both integrations = False."""
    result = get_user_integrations(user)
    assert result.google_connected is False
    assert result.microsoft_connected is False


# ===========================================================================
# U-B-40 — get_user_integrations: both tokens set → both True
# ===========================================================================
def test_get_user_integrations_both_connected(user: User) -> None:
    """A user with both refresh tokens set must have both integrations = True."""
    user.google_refresh_token = "google-rt-token"
    user.microsoft_refresh_token = "ms-rt-token"
    result = get_user_integrations(user)
    assert result.google_connected is True
    assert result.microsoft_connected is True


# ===========================================================================
# U-B-41 — get_user_integrations: blank token treated as disconnected
# ===========================================================================
def test_get_user_integrations_blank_token_treated_as_disconnected(user: User) -> None:
    """A blank (whitespace-only) token must be treated as disconnected."""
    user.google_refresh_token = "   "
    user.microsoft_refresh_token = ""
    result = get_user_integrations(user)
    assert result.google_connected is False
    assert result.microsoft_connected is False
