"""Integration tests for Organization Management and Google SSO HTTP endpoints."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")

from config.database import get_db
from main import app
from middlewares.tenant_context import TenantContext, get_tenant_context
from models.auth_model import Base as AuthBase, Organization, OrganizationMembership, User
from services.organization_service import auth0_sync


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    AuthBase.metadata.create_all(bind=eng)
    yield eng
    AuthBase.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture()
def db_session(engine) -> Generator[Session, None, None]:
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ===========================================================================
# 1. Platform Super Admin Endpoints
# ===========================================================================

def test_create_organization_super_admin_endpoint(client: TestClient, db_session: Session):
    """Super Admin can create a new organization."""
    super_admin = User(id="sa-api-1", email="sa@example.com", username="sa1", password_hash="h", role="super_admin", is_active=True, is_verified=True)
    db_session.add(super_admin)
    db_session.commit()

    def override_tenant():
        return TenantContext(user=super_admin, organization=None, role="super_admin")

    app.dependency_overrides[get_tenant_context] = override_tenant

    resp = client.post(
        "/v1/organizations",
        json={"name": "Acme Ventures", "display_name": "Acme Ventures Inc", "google_sso_enabled": True},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Acme Ventures"
    assert data["google_sso_enabled"] is True


def test_create_organization_forbidden_for_normal_user(client: TestClient, db_session: Session):
    """Normal user attempting to create an organization receives 403 Forbidden."""
    normal_user = User(id="user-norm-1", email="user@example.com", username="norm1", password_hash="h", role="user", is_active=True, is_verified=True)
    db_session.add(normal_user)
    db_session.commit()

    def override_tenant():
        return TenantContext(user=normal_user, organization=None, role="member")

    app.dependency_overrides[get_tenant_context] = override_tenant

    resp = client.post(
        "/v1/organizations",
        json={"name": "Hacker Org"},
    )
    assert resp.status_code == 403


# ===========================================================================
# 2. Organization Admin Endpoints
# ===========================================================================

def test_org_admin_self_serve_settings_and_members(client: TestClient, db_session: Session):
    """Org Admin can toggle Google SSO and manage members via /v1/organizations/me/*."""
    org = Organization(id="org-api-1", name="Alpha Studio", display_name="Alpha Studio LLC", is_active=True, google_sso_enabled=False)
    org_admin_user = User(id="oa-1", email="lead@alpha.com", username="lead_alpha", password_hash="h", role="user", is_active=True, is_verified=True)
    colleague_user = User(id="user-colleague", email="dev@alpha.com", username="dev_alpha", password_hash="h", role="user", is_active=True, is_verified=True)
    db_session.add_all([org, org_admin_user, colleague_user])
    db_session.commit()

    def override_tenant():
        return TenantContext(user=org_admin_user, organization=org, role="org_admin")

    app.dependency_overrides[get_tenant_context] = override_tenant

    # 1. Get profile
    resp = client.get("/v1/organizations/me/profile")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Alpha Studio"

    # 2. Toggle Google SSO ON
    resp = client.patch("/v1/organizations/me/settings", json={"google_sso_enabled": True})
    assert resp.status_code == 200
    assert resp.json()["google_sso_enabled"] is True

    # 3. Add colleague as member
    resp = client.post(
        "/v1/organizations/me/members",
        json={"user_identifier": "dev@alpha.com", "role": "member"},
    )
    assert resp.status_code == 201
    mem_data = resp.json()
    assert mem_data["email"] == "dev@alpha.com"
    assert mem_data["role"] == "member"
    membership_id = mem_data["membership_id"]

    # 4. List members
    resp = client.get("/v1/organizations/me/members")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 5. Update role to org_admin
    resp = client.patch(f"/v1/organizations/me/members/{membership_id}", json={"role": "org_admin"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "org_admin"

    # 6. Delete member
    resp = client.delete(f"/v1/organizations/me/members/{membership_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "removed"
