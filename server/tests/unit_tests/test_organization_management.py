"""Unit tests for Phase 4: Organization Management and Google SSO Configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

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

from models.auth_model import Base as AuthBase, Organization, OrganizationMembership, User
from schemas.organization_schema import (
    AddMemberRequest,
    OrganizationCreateRequest,
    OrganizationUpdateRequest,
    OrgSettingsUpdateRequest,
    UpdateMemberRoleRequest,
)
from services.organization_service import (
    add_organization_member,
    auth0_sync,
    create_organization,
    get_organization_by_id,
    list_organization_members,
    list_organizations,
    remove_organization_member,
    update_member_role,
    update_org_settings_self_serve,
    update_organization,
)


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Isolated in-memory SQLite database session."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    AuthBase.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        AuthBase.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def super_admin(db_session: Session) -> User:
    u = User(id="sa-1", email="admin@platform.com", username="superadmin", password_hash="h", role="super_admin", is_verified=True, is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def org_user(db_session: Session) -> User:
    u = User(id="user-1", email="alice@test.com", username="alice", password_hash="h", role="user", is_verified=True, is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def second_user(db_session: Session) -> User:
    u = User(id="user-2", email="bob@test.com", username="bob", password_hash="h", role="user", is_verified=True, is_active=True)
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


# ===========================================================================
# 1. Organization Creation & Administration (Super Admin)
# ===========================================================================

def test_create_organization_happy_path(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """Super admin creates an organization with Google SSO enabled."""
    mock_sync_conn = MagicMock()
    monkeypatch.setattr(auth0_sync, "sync_google_sso_connection", mock_sync_conn)

    payload = OrganizationCreateRequest(
        name="Fintech Global",
        display_name="Fintech Global Inc.",
        auth0_org_id="org_fintech_123",
        google_sso_enabled=True,
    )
    org = create_organization(db_session, payload)

    assert org.id is not None
    assert org.name == "Fintech Global"
    assert org.display_name == "Fintech Global Inc."
    assert org.auth0_org_id == "org_fintech_123"
    assert org.is_active is True
    assert org.google_sso_enabled is True
    mock_sync_conn.assert_called_once_with("org_fintech_123", True)


def test_create_organization_duplicate_name_rejected(db_session: Session):
    """Creating an organization with an existing name raises 409 Conflict."""
    payload1 = OrganizationCreateRequest(name="Duplicate Corp")
    create_organization(db_session, payload1)

    payload2 = OrganizationCreateRequest(name="duplicate corp")
    with pytest.raises(HTTPException) as exc:
        create_organization(db_session, payload2)
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


def test_list_and_get_organizations(db_session: Session):
    """Super Admin can list and retrieve organizations by ID or Auth0 Org ID."""
    org1 = create_organization(db_session, OrganizationCreateRequest(name="Org One", auth0_org_id="org_1"))
    org2 = create_organization(db_session, OrganizationCreateRequest(name="Org Two", auth0_org_id="org_2"))

    orgs = list_organizations(db_session, page=1, limit=10)
    assert len(orgs) == 2

    # Lookup by internal UUID
    fetched = get_organization_by_id(db_session, org1.id)
    assert fetched.name == "Org One"

    # Lookup by Auth0 Org ID
    fetched_auth0 = get_organization_by_id(db_session, "org_2")
    assert fetched_auth0.name == "Org Two"


def test_update_organization_suspend_and_activate(db_session: Session):
    """Super Admin can suspend and activate an organization."""
    org = create_organization(db_session, OrganizationCreateRequest(name="Suspend Me"))
    assert org.is_active is True

    # Suspend
    updated = update_organization(db_session, org.id, OrganizationUpdateRequest(is_active=False))
    assert updated.is_active is False

    # Reactivate
    reactivated = update_organization(db_session, org.id, OrganizationUpdateRequest(is_active=True))
    assert reactivated.is_active is True


# ===========================================================================
# 2. Google SSO Toggling (Org Admin Self-Serve & Super Admin)
# ===========================================================================

def test_toggle_google_sso_independent_per_org(db_session: Session, monkeypatch: pytest.MonkeyPatch):
    """Org A enables Google SSO; Org B keeps it disabled. States remain isolated."""
    mock_sync = MagicMock()
    monkeypatch.setattr(auth0_sync, "sync_google_sso_connection", mock_sync)

    org_a = create_organization(db_session, OrganizationCreateRequest(name="Org A", auth0_org_id="org_auth0_a", google_sso_enabled=False))
    org_b = create_organization(db_session, OrganizationCreateRequest(name="Org B", auth0_org_id="org_auth0_b", google_sso_enabled=False))

    # Org Admin toggles Google SSO ON for Org A
    update_org_settings_self_serve(db_session, org_a, OrgSettingsUpdateRequest(google_sso_enabled=True))
    assert org_a.google_sso_enabled is True
    assert org_b.google_sso_enabled is False
    mock_sync.assert_called_with("org_auth0_a", True)

    # Org Admin toggles Google SSO OFF for Org A
    update_org_settings_self_serve(db_session, org_a, OrgSettingsUpdateRequest(google_sso_enabled=False))
    assert org_a.google_sso_enabled is False
    mock_sync.assert_called_with("org_auth0_a", False)


# ===========================================================================
# 3. Organization Membership Management
# ===========================================================================

def test_add_and_list_organization_members(db_session: Session, org_user: User, second_user: User):
    """Org Admin adds members with specific roles and lists them."""
    org = create_organization(db_session, OrganizationCreateRequest(name="Team Workspace"))

    # Add Alice as Org Admin
    m1 = add_organization_member(db_session, org.id, AddMemberRequest(user_identifier=org_user.email, role="org_admin"))
    assert m1.user_id == org_user.id
    assert m1.role == "org_admin"
    assert m1.is_active is True

    # Add Bob as Normal Member
    m2 = add_organization_member(db_session, org.id, AddMemberRequest(user_identifier=second_user.id, role="member"))
    assert m2.user_id == second_user.id
    assert m2.role == "member"

    members = list_organization_members(db_session, org.id)
    assert len(members) == 2
    roles = {m.user_id: m.role for m in members}
    assert roles[org_user.id] == "org_admin"
    assert roles[second_user.id] == "member"


def test_add_duplicate_member_rejected(db_session: Session, org_user: User):
    """Adding an already active member raises 409 Conflict."""
    org = create_organization(db_session, OrganizationCreateRequest(name="Single Team"))
    add_organization_member(db_session, org.id, AddMemberRequest(user_identifier=org_user.email, role="member"))

    with pytest.raises(HTTPException) as exc:
        add_organization_member(db_session, org.id, AddMemberRequest(user_identifier=org_user.username, role="member"))
    assert exc.value.status_code == 409
    assert "already an active member" in exc.value.detail


def test_update_member_role_and_remove(db_session: Session, org_user: User):
    """Modify a member's role and then remove the member."""
    org = create_organization(db_session, OrganizationCreateRequest(name="Role Testing"))
    m = add_organization_member(db_session, org.id, AddMemberRequest(user_identifier=org_user.email, role="member"))
    assert m.role == "member"

    # Promote to org_admin
    updated = update_member_role(db_session, org.id, m.membership_id, UpdateMemberRoleRequest(role="org_admin"))
    assert updated.role == "org_admin"

    # Remove member
    remove_organization_member(db_session, org.id, m.membership_id)
    remaining = list_organization_members(db_session, org.id)
    assert len(remaining) == 0
