"""Unit tests for Organization, OrganizationMembership, and User models (Phase 1)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models.auth_model import Base, Organization, OrganizationMembership, User
from models.file_model import Dataset, IngestedFile, IngestedFileProviderType


@pytest.fixture
def db_session():
    """Provide an in-memory SQLite database session with current models."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_create_organization_and_memberships(db_session):
    """Verify organization creation, memberships, and multiple organizations per user."""
    # Create organizations
    org_a = Organization(
        name="Org Alpha",
        display_name="Organization Alpha",
        google_sso_enabled=True,
    )
    org_b = Organization(
        name="Org Beta",
        display_name="Organization Beta",
        google_sso_enabled=False,
    )
    db_session.add_all([org_a, org_b])
    db_session.commit()

    assert org_a.id is not None
    assert org_a.is_active is True
    assert org_a.google_sso_enabled is True
    assert org_b.google_sso_enabled is False

    # Create a user
    user = User(
        email="multi_org_user@example.com",
        username="multiorg",
        password_hash="pbkdf2_dummy_hash",
        auth0_sub="auth0|user_12345",
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    # Add user to both Org A (as org_admin) and Org B (as member)
    mem_a = OrganizationMembership(
        organization_id=org_a.id,
        user_id=user.id,
        role="org_admin",
    )
    mem_b = OrganizationMembership(
        organization_id=org_b.id,
        user_id=user.id,
        role="member",
    )
    db_session.add_all([mem_a, mem_b])
    db_session.commit()

    # Verify user memberships
    db_session.refresh(user)
    assert len(user.memberships) == 2
    roles_by_org = {m.organization_id: m.role for m in user.memberships}
    assert roles_by_org[org_a.id] == "org_admin"
    assert roles_by_org[org_b.id] == "member"


def test_dataset_and_ingested_file_organization_scoping(db_session):
    """Verify datasets and ingested files can be associated with an organization."""
    org = Organization(name="Scoping Org", display_name="Scoping Org")
    user = User(
        email="scope_user@example.com",
        username="scopeuser",
        password_hash="pbkdf2_dummy_hash",
        role="user",
    )
    db_session.add_all([org, user])
    db_session.commit()

    dataset = Dataset(
        user_id=user.id,
        organization_id=org.id,
        name="Tenant Dataset",
        status="Created",
    )
    db_session.add(dataset)
    db_session.commit()

    file_record = IngestedFile(
        user_id=user.id,
        organization_id=org.id,
        dataset_id=dataset.id,
        provider=IngestedFileProviderType.Local,
        file_path="/files/doc.pdf",
        filename="doc.pdf",
        status="completed",
    )
    db_session.add(file_record)
    db_session.commit()

    assert dataset.organization_id == org.id
    assert dataset.organization.name == "Scoping Org"
    assert file_record.organization_id == org.id
    assert file_record.organization.name == "Scoping Org"
