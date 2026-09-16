"""Unit tests for Phase 3: Tenant Authorization, Scoping, and IDOR/BOLA Protection."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Setup path
SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-tenant")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-tenant")

from middlewares.tenant_context import TenantContext, get_tenant_context
from models.auth_model import Base as AuthBase, Organization, OrganizationMembership, User
from models.file_model import Base as FileBase, Dataset, IngestedFile, IngestedFileProviderType
from schemas.file_schema import DatasetAttachFileRequest, DatasetCreate, DatasetUpdate, UploadInitRequest
from services.file_services import (
    attach_file_to_dataset,
    create_dataset,
    delete_dataset,
    delete_user_upload,
    get_dataset_by_id,
    get_datasets,
    initialize_upload,
    list_user_uploads,
    update_dataset,
)
from utils.errors import DatasetAccessError, DatasetNotFoundError, FileNotFoundError, FileOwnershipError, UploadNotFoundError, UploadOwnershipError


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Isolated in-memory SQLite database session."""
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
def seeded_env(db_session: Session):
    """Seed two organizations, users with distinct memberships, and a super admin."""
    # Organizations
    org_a = Organization(id="org-a", name="Acme Corp", auth0_org_id="org_auth0_a", is_active=True)
    org_b = Organization(id="org-b", name="Beta LLC", auth0_org_id="org_auth0_b", is_active=True)
    org_inactive = Organization(id="org-inactive", name="Inactive Co", auth0_org_id="org_auth0_inact", is_active=False)
    db_session.add_all([org_a, org_b, org_inactive])

    # Users
    user_a = User(id="user-a", email="alice@acme.com", username="alice", password_hash="h", role="user", is_verified=True, is_active=True)
    user_b = User(id="user-b", email="bob@beta.com", username="bob", password_hash="h", role="user", is_verified=True, is_active=True)
    user_multi = User(id="user-multi", email="multi@corp.com", username="multi", password_hash="h", role="user", is_verified=True, is_active=True)
    super_admin = User(id="user-sa", email="admin@platform.com", username="admin", password_hash="h", role="super_admin", is_verified=True, is_active=True)
    db_session.add_all([user_a, user_b, user_multi, super_admin])

    # Memberships
    m_a = OrganizationMembership(id="mem-a", organization_id="org-a", user_id="user-a", role="member", is_active=True)
    m_b = OrganizationMembership(id="mem-b", organization_id="org-b", user_id="user-b", role="org_admin", is_active=True)
    m_multi_a = OrganizationMembership(id="mem-ma", organization_id="org-a", user_id="user-multi", role="member", is_active=True)
    m_multi_b = OrganizationMembership(id="mem-mb", organization_id="org-b", user_id="user-multi", role="org_admin", is_active=True)
    db_session.add_all([m_a, m_b, m_multi_a, m_multi_b])

    db_session.commit()
    return {
        "org_a": org_a,
        "org_b": org_b,
        "org_inactive": org_inactive,
        "user_a": user_a,
        "user_b": user_b,
        "user_multi": user_multi,
        "super_admin": super_admin,
    }


# ===========================================================================
# 1. TenantContext Resolution & Security Checks
# ===========================================================================

def test_tenant_context_default_active_membership(db_session: Session, seeded_env, monkeypatch: pytest.MonkeyPatch):
    """User without X-Organization-ID resolves to their default active membership."""
    user_a = seeded_env["user_a"]
    monkeypatch.setattr("middlewares.tenant_context.get_current_user", lambda db, tok: user_a)

    ctx = get_tenant_context(authorization="Bearer valid-token", x_organization_id=None, db=db_session)
    assert ctx.user.id == "user-a"
    assert ctx.organization_id == "org-a"
    assert ctx.role == "member"
    assert not ctx.is_org_admin
    assert not ctx.is_super_admin


def test_tenant_context_explicit_header_matching_membership(db_session: Session, seeded_env, monkeypatch: pytest.MonkeyPatch):
    """Multi-org user selecting Org B via X-Organization-ID gets Org B context."""
    user_multi = seeded_env["user_multi"]
    monkeypatch.setattr("middlewares.tenant_context.get_current_user", lambda db, tok: user_multi)

    ctx = get_tenant_context(authorization="Bearer valid-token", x_organization_id="org-b", db=db_session)
    assert ctx.user.id == "user-multi"
    assert ctx.organization_id == "org-b"
    assert ctx.role == "org_admin"
    assert ctx.is_org_admin


def test_tenant_context_explicit_header_by_auth0_org_id(db_session: Session, seeded_env, monkeypatch: pytest.MonkeyPatch):
    """X-Organization-ID can match Auth0 organization identifier (e.g. org_auth0_a)."""
    user_a = seeded_env["user_a"]
    monkeypatch.setattr("middlewares.tenant_context.get_current_user", lambda db, tok: user_a)

    ctx = get_tenant_context(authorization="Bearer valid-token", x_organization_id="org_auth0_a", db=db_session)
    assert ctx.organization_id == "org-a"


def test_tenant_context_rejects_unauthorized_org_access(db_session: Session, seeded_env, monkeypatch: pytest.MonkeyPatch):
    """User A cannot access Org B by manipulating X-Organization-ID header (403)."""
    user_a = seeded_env["user_a"]
    monkeypatch.setattr("middlewares.tenant_context.get_current_user", lambda db, tok: user_a)

    with pytest.raises(HTTPException) as exc:
        get_tenant_context(authorization="Bearer valid-token", x_organization_id="org-b", db=db_session)
    assert exc.value.status_code == 403
    assert "User does not have an active membership" in exc.value.detail


def test_tenant_context_super_admin_cannot_access_tenant_without_membership(db_session: Session, seeded_env, monkeypatch: pytest.MonkeyPatch):
    """Super Admin cannot access an organization's resources without explicit membership."""
    super_admin = seeded_env["super_admin"]
    monkeypatch.setattr("middlewares.tenant_context.get_current_user", lambda db, tok: super_admin)

    with pytest.raises(HTTPException) as exc:
        get_tenant_context(authorization="Bearer valid-token", x_organization_id="org-a", db=db_session)
    assert exc.value.status_code == 403
    assert "User does not have an active membership" in exc.value.detail


def test_tenant_context_rejects_inactive_org(db_session: Session, seeded_env, monkeypatch: pytest.MonkeyPatch):
    """Accessing an inactive organization is rejected with 403."""
    user_a = seeded_env["user_a"]
    monkeypatch.setattr("middlewares.tenant_context.get_current_user", lambda db, tok: user_a)

    with pytest.raises(HTTPException) as exc:
        get_tenant_context(authorization="Bearer valid-token", x_organization_id="org-inactive", db=db_session)
    assert exc.value.status_code == 403
    assert "inactive" in exc.value.detail.lower()


def test_tenant_context_rejects_nonexistent_org(db_session: Session, seeded_env, monkeypatch: pytest.MonkeyPatch):
    """Accessing a nonexistent organization returns 404."""
    user_a = seeded_env["user_a"]
    monkeypatch.setattr("middlewares.tenant_context.get_current_user", lambda db, tok: user_a)

    with pytest.raises(HTTPException) as exc:
        get_tenant_context(authorization="Bearer valid-token", x_organization_id="org-unknown", db=db_session)
    assert exc.value.status_code == 404


# ===========================================================================
# 2. Multi-Tenant Dataset Isolation & IDOR/BOLA Protection
# ===========================================================================

def test_dataset_scoping_creation_and_listing(db_session: Session, seeded_env):
    """Datasets created under Org A are isolated from Org B."""
    user_a = seeded_env["user_a"]
    user_b = seeded_env["user_b"]

    ds_a = create_dataset(db_session, user_a, DatasetCreate(name="Q1 Financials", description="Org A", language="English"), organization_id="org-a")
    ds_b = create_dataset(db_session, user_b, DatasetCreate(name="Q1 Financials", description="Org B", language="English"), organization_id="org-b")

    assert ds_a.organization_id == "org-a"
    assert ds_b.organization_id == "org-b"
    assert ds_a.id != ds_b.id

    # List Org A datasets: only returns ds_a
    list_a = get_datasets(db_session, user_a, organization_id="org-a")
    assert len(list_a) == 1
    assert list_a[0].id == ds_a.id

    # List Org B datasets: only returns ds_b
    list_b = get_datasets(db_session, user_b, organization_id="org-b")
    assert len(list_b) == 1
    assert list_b[0].id == ds_b.id


def test_dataset_get_by_id_cross_tenant_idor_rejected(db_session: Session, seeded_env):
    """User in Org A cannot access Dataset belonging to Org B by ID."""
    user_a = seeded_env["user_a"]
    user_b = seeded_env["user_b"]

    ds_b = create_dataset(db_session, user_b, DatasetCreate(name="Confidential Org B", language="English"), organization_id="org-b")

    # Access under Org A context raises DatasetAccessError (403)
    with pytest.raises(DatasetAccessError):
        get_dataset_by_id(db_session, user_a, ds_b.id, organization_id="org-a")


def test_dataset_update_cross_tenant_rejected(db_session: Session, seeded_env):
    """User in Org A cannot update Dataset belonging to Org B."""
    user_a = seeded_env["user_a"]
    user_b = seeded_env["user_b"]

    ds_b = create_dataset(db_session, user_b, DatasetCreate(name="Org B Original", language="English"), organization_id="org-b")

    with pytest.raises(DatasetAccessError):
        update_dataset(db_session, user_a, ds_b.id, DatasetUpdate(name="Tampered Name"), organization_id="org-a")


def test_dataset_delete_cross_tenant_rejected(db_session: Session, seeded_env):
    """User in Org A cannot delete Dataset belonging to Org B."""
    user_a = seeded_env["user_a"]
    user_b = seeded_env["user_b"]

    ds_b = create_dataset(db_session, user_b, DatasetCreate(name="Org B To Delete", language="English"), organization_id="org-b")

    with pytest.raises(DatasetAccessError):
        delete_dataset(db_session, user_a, ds_b.id, organization_id="org-a")


# ===========================================================================
# 3. Multi-Tenant File Upload & Attachment IDOR/BOLA Protection
# ===========================================================================

def test_file_attachment_cross_tenant_idor_rejected(db_session: Session, seeded_env):
    """User in Org A cannot attach a file belonging to Org B into an Org A dataset."""
    user_a = seeded_env["user_a"]
    user_b = seeded_env["user_b"]

    ds_a = create_dataset(db_session, user_a, DatasetCreate(name="Org A Data", language="English"), organization_id="org-a")

    # Ingested file belonging to Org B
    file_b = IngestedFile(
        id="file-b-secret",
        user_id=user_b.id,
        organization_id="org-b",
        provider=IngestedFileProviderType.Local,
        file_path="org_b_secret.pdf",
        filename="org_b_secret.pdf",
        physical_path="/tmp/org_b.pdf",
        file_size_bytes=1024,
        master_hash="a" * 64,
        status="completed",
    )
    db_session.add(file_b)
    db_session.commit()

    # User in Org A attempts to attach Org B file into Org A dataset
    with pytest.raises(FileOwnershipError):
        attach_file_to_dataset(
            db_session,
            user_a,
            ds_a.id,
            DatasetAttachFileRequest(file_id=file_b.id),
            organization_id="org-a",
        )


def test_file_attachment_same_tenant_success(db_session: Session, seeded_env):
    """User in Org A can successfully attach a file belonging to Org A."""
    user_a = seeded_env["user_a"]
    ds_a = create_dataset(db_session, user_a, DatasetCreate(name="Org A Data", language="English"), organization_id="org-a")

    file_a = IngestedFile(
        id="file-a-doc",
        user_id=user_a.id,
        organization_id="org-a",
        provider=IngestedFileProviderType.Local,
        file_path="org_a_doc.pdf",
        filename="org_a_doc.pdf",
        physical_path="/tmp/org_a.pdf",
        file_size_bytes=2048,
        master_hash="b" * 64,
        status="completed",
    )
    db_session.add(file_a)
    db_session.commit()

    resp = attach_file_to_dataset(
        db_session,
        user_a,
        ds_a.id,
        DatasetAttachFileRequest(file_id=file_a.id),
        organization_id="org-a",
    )
    assert resp.status == "attached"
    assert resp.file_id == file_a.id

    # Verify dataset tree shows attached file
    tree = list_user_uploads(db_session, user_a, dataset_id=ds_a.id, organization_id="org-a")
    assert len(tree.children) == 1
    assert tree.children[0].name == "org_a_doc.pdf"


def test_file_delete_cross_tenant_rejected(db_session: Session, seeded_env):
    """User in Org A cannot delete an uploaded file belonging to Org B."""
    user_a = seeded_env["user_a"]
    user_b = seeded_env["user_b"]

    file_b = IngestedFile(
        id="file-b-delete-target",
        user_id=user_b.id,
        organization_id="org-b",
        provider=IngestedFileProviderType.Local,
        file_path="org_b.pdf",
        filename="org_b.pdf",
        status="completed",
    )
    db_session.add(file_b)
    db_session.commit()

    with pytest.raises(UploadOwnershipError):
        delete_user_upload(db_session, user_a, file_b.id, organization_id="org-a")
