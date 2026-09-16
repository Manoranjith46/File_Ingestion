"""Business logic for Multi-Organization management, membership lifecycle, and Auth0 synchronization."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import requests as http_requests
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from config.auth0_config import auth0_settings
from models.auth_model import Organization, OrganizationMembership, User
from schemas.organization_schema import (
    AddMemberRequest,
    OrganizationCreateRequest,
    OrganizationMemberResponse,
    OrganizationResponse,
    OrganizationUpdateRequest,
    OrgSettingsUpdateRequest,
    UpdateMemberRoleRequest,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional Auth0 Management API Sync Client
# ---------------------------------------------------------------------------

class Auth0ManagementSync:
    """Synchronizes organization creation and connection settings with Auth0 Management API when configured."""

    def __init__(self):
        self._cached_token: str | None = None

    def get_management_token(self) -> str | None:
        """Obtain M2M access token for Auth0 Management API."""
        domain = auth0_settings.domain
        client_id = auth0_settings.client_id
        client_secret = auth0_settings.client_secret
        if not (domain and client_id and client_secret):
            return None

        url = f"https://{domain}/oauth/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "audience": f"https://{domain}/api/v2/",
        }
        try:
            resp = http_requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("access_token")
            logger.warning(f"Auth0 Management API token request failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.warning(f"Failed to communicate with Auth0 token endpoint: {e}")
        return None

    def sync_organization_create(self, name: str, display_name: str | None = None) -> str | None:
        """Create an organization in Auth0 if configured and return its auth0_org_id."""
        token = self.get_management_token()
        if not token:
            return None

        domain = auth0_settings.domain
        url = f"https://{domain}/api/v2/organizations"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        # Auth0 org names must be lowercase alphanumeric, hyphens allowed
        auth0_name = name.strip().lower().replace(" ", "-").replace("_", "-")
        payload = {
            "name": auth0_name,
            "display_name": display_name or name,
        }
        try:
            resp = http_requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                data = resp.json()
                return data.get("id")
            logger.warning(f"Auth0 organization creation failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.warning(f"Failed to create Auth0 organization: {e}")
        return None

    def sync_google_sso_connection(self, auth0_org_id: str, enabled: bool) -> None:
        """Enable or disable Google connection on the Auth0 Organization."""
        token = self.get_management_token()
        if not (token and auth0_org_id):
            return

        domain = auth0_settings.domain
        # List connections to find google-oauth2
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            conn_resp = http_requests.get(f"https://{domain}/api/v2/connections?strategy=google-oauth2", headers=headers, timeout=10)
            if conn_resp.status_code != 200:
                return
            conns = conn_resp.json()
            if not conns:
                return
            google_conn_id = conns[0].get("id")
            if not google_conn_id:
                return

            if enabled:
                # Enable connection on org
                url = f"https://{domain}/api/v2/organizations/{auth0_org_id}/enabled_connections"
                http_requests.post(url, json={"connection_id": google_conn_id, "assign_membership_on_login": False}, headers=headers, timeout=10)
            else:
                # Disable connection on org
                url = f"https://{domain}/api/v2/organizations/{auth0_org_id}/enabled_connections/{google_conn_id}"
                http_requests.delete(url, headers=headers, timeout=10)
        except Exception as e:
            logger.warning(f"Failed to sync Google SSO connection state with Auth0: {e}")


auth0_sync = Auth0ManagementSync()


# ---------------------------------------------------------------------------
# Organization CRUD & Administration
# ---------------------------------------------------------------------------

def create_organization(db: Session, payload: OrganizationCreateRequest) -> Organization:
    """Create a new tenant organization (Super Admin only)."""
    clean_name = payload.name.strip()
    existing = db.query(Organization).filter(func.lower(Organization.name) == clean_name.lower()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An organization with name '{clean_name}' already exists.",
        )

    auth0_org_id = payload.auth0_org_id.strip() if payload.auth0_org_id else None
    if not auth0_org_id:
        # Attempt to provision via Auth0 Management API if client credentials configured
        synced_id = auth0_sync.sync_organization_create(clean_name, payload.display_name)
        if synced_id:
            auth0_org_id = synced_id

    org = Organization(
        name=clean_name,
        display_name=payload.display_name.strip() if payload.display_name else clean_name,
        auth0_org_id=auth0_org_id,
        is_active=True,
        google_sso_enabled=payload.google_sso_enabled,
    )
    db.add(org)
    db.commit()
    db.refresh(org)

    # If initial admin credentials are provided, auto-provision the user & assign as org_admin
    if payload.admin_email and payload.admin_password:
        from helpers.jwt import hash_password
        from services.auth_services import request_otp
        from schemas.auth_schema import OtpRequest

        admin_email_clean = str(payload.admin_email).strip().lower()
        admin_user = db.query(User).filter(func.lower(User.email) == admin_email_clean).first()
        if not admin_user:
            admin_user = User(
                email=admin_email_clean,
                username=payload.admin_username.strip() if payload.admin_username else admin_email_clean.split("@")[0],
                full_name=payload.admin_name.strip() if payload.admin_name else "Organization Admin",
                password_hash=hash_password(payload.admin_password),
                role="user",
                is_verified=False,
                is_active=True,
                auth_provider="local",
            )
            db.add(admin_user)
            db.commit()
            db.refresh(admin_user)
            try:
                request_otp(db, OtpRequest(email=admin_user.email))
                logger.info(f"Generated initial activation OTP for Org Admin: {admin_user.email}")
            except Exception as e:
                logger.warning(f"Failed to generate initial OTP for admin {admin_user.email}: {e}")

        # Ensure active org_admin membership exists
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == org.id,
                OrganizationMembership.user_id == admin_user.id,
            )
            .first()
        )
        if not membership:
            membership = OrganizationMembership(
                organization_id=org.id,
                user_id=admin_user.id,
                role="org_admin",
                is_active=True,
            )
            db.add(membership)
            db.commit()

    if org.auth0_org_id and org.google_sso_enabled:
        auth0_sync.sync_google_sso_connection(org.auth0_org_id, True)

    return org


def list_organizations(
    db: Session,
    page: int = 1,
    limit: int = 50,
    include_inactive: bool = True,
) -> list[Organization]:
    """List organizations across the platform (Super Admin only)."""
    offset = (page - 1) * limit
    query = db.query(Organization)
    if not include_inactive:
        query = query.filter(Organization.is_active == True)
    return query.order_by(Organization.created_at.desc()).offset(offset).limit(limit).all()


def get_organization_by_id(db: Session, org_id: str) -> Organization:
    """Retrieve an organization by its ID or Auth0 Org ID."""
    clean_id = org_id.strip()
    org = (
        db.query(Organization)
        .filter((Organization.id == clean_id) | (Organization.auth0_org_id == clean_id))
        .one_or_none()
    )
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


def update_organization(db: Session, org_id: str, payload: OrganizationUpdateRequest) -> Organization:
    """Update organization settings, active status, or Google SSO enablement (Super Admin)."""
    org = get_organization_by_id(db, org_id)

    if payload.display_name is not None:
        org.display_name = payload.display_name.strip()

    if payload.auth0_org_id is not None:
        org.auth0_org_id = payload.auth0_org_id.strip() or None

    if payload.is_active is not None:
        org.is_active = payload.is_active

    if payload.google_sso_enabled is not None:
        org.google_sso_enabled = payload.google_sso_enabled
        if org.auth0_org_id:
            auth0_sync.sync_google_sso_connection(org.auth0_org_id, org.google_sso_enabled)

    db.commit()
    db.refresh(org)
    return org


def update_org_settings_self_serve(db: Session, org: Organization, payload: OrgSettingsUpdateRequest) -> Organization:
    """Allow an Organization Admin to update their organization's display name and toggle Google SSO."""
    if payload.display_name is not None:
        org.display_name = payload.display_name.strip()

    if payload.google_sso_enabled is not None:
        org.google_sso_enabled = payload.google_sso_enabled
        if org.auth0_org_id:
            auth0_sync.sync_google_sso_connection(org.auth0_org_id, org.google_sso_enabled)

    db.commit()
    db.refresh(org)
    return org


# ---------------------------------------------------------------------------
# Organization Membership Management
# ---------------------------------------------------------------------------

def list_organization_members(db: Session, org_id: str) -> list[OrganizationMemberResponse]:
    """List all members belonging to an organization."""
    memberships = (
        db.query(OrganizationMembership)
        .join(User, OrganizationMembership.user_id == User.id)
        .filter(OrganizationMembership.organization_id == org_id)
        .order_by(OrganizationMembership.created_at.asc())
        .all()
    )

    result = []
    for m in memberships:
        result.append(
            OrganizationMemberResponse(
                membership_id=m.id,
                user_id=m.user.id,
                email=m.user.email,
                username=m.user.username,
                full_name=m.user.full_name,
                role=m.role,
                is_active=m.is_active and m.user.is_active,
                created_at=m.created_at,
            )
        )
    return result


def add_organization_member(db: Session, org_id: str, payload: AddMemberRequest) -> OrganizationMemberResponse:
    """Add a user to an organization with a designated role, creating the user if needed."""
    clean_ident = (payload.user_identifier or str(payload.email or "")).strip()
    if not clean_ident:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'user_identifier' or 'email' must be provided.",
        )

    user = (
        db.query(User)
        .filter(
            (User.id == clean_ident)
            | (func.lower(User.email) == clean_ident.lower())
            | (User.username == clean_ident)
        )
        .one_or_none()
    )

    # If user does not exist yet, allow provisioning with temporary_password
    if not user:
        if payload.temporary_password:
            from helpers.jwt import hash_password
            from services.auth_services import request_otp
            from schemas.auth_schema import OtpRequest

            email_to_use = clean_ident if "@" in clean_ident else f"{clean_ident}@example.com"
            user = User(
                email=email_to_use.lower(),
                username=clean_ident.split("@")[0],
                full_name=payload.user_name.strip() if payload.user_name else clean_ident.split("@")[0],
                password_hash=hash_password(payload.temporary_password),
                role="user",
                is_verified=False,
                is_active=True,
                auth_provider="local",
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            try:
                request_otp(db, OtpRequest(email=user.email))
                logger.info(f"Generated initial activation OTP for member: {user.email}")
            except Exception as e:
                logger.warning(f"Failed to generate OTP for new member {user.email}: {e}")
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User '{clean_ident}' not found. Provide 'temporary_password' to create a new user.",
            )

    target_role = payload.role.strip().lower()
    if target_role not in ("org_admin", "member"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be either 'org_admin' or 'member'.",
        )

    existing_membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == user.id,
        )
        .one_or_none()
    )
    if existing_membership:
        if not existing_membership.is_active:
            existing_membership.is_active = True
            existing_membership.role = target_role
            db.commit()
            db.refresh(existing_membership)
            return OrganizationMemberResponse(
                membership_id=existing_membership.id,
                user_id=user.id,
                email=user.email,
                username=user.username,
                full_name=user.full_name,
                role=existing_membership.role,
                is_active=existing_membership.is_active,
                created_at=existing_membership.created_at,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already an active member of this organization.",
        )

    membership = OrganizationMembership(
        organization_id=org_id,
        user_id=user.id,
        role=target_role,
        is_active=True,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)

    return OrganizationMemberResponse(
        membership_id=membership.id,
        user_id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        role=membership.role,
        is_active=membership.is_active,
        created_at=membership.created_at,
    )


def update_member_role(
    db: Session,
    org_id: str,
    membership_id: str,
    payload: UpdateMemberRoleRequest,
) -> OrganizationMemberResponse:
    """Modify the role of an existing organization member."""
    target_role = payload.role.strip().lower()
    if target_role not in ("org_admin", "member"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be either 'org_admin' or 'member'.",
        )

    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.id == membership_id,
            OrganizationMembership.organization_id == org_id,
        )
        .one_or_none()
    )
    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership record not found.")

    membership.role = target_role
    db.commit()
    db.refresh(membership)

    return OrganizationMemberResponse(
        membership_id=membership.id,
        user_id=membership.user.id,
        email=membership.user.email,
        username=membership.user.username,
        full_name=membership.user.full_name,
        role=membership.role,
        is_active=membership.is_active,
        created_at=membership.created_at,
    )


def remove_organization_member(db: Session, org_id: str, membership_id: str) -> None:
    """Remove a user membership from an organization."""
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.id == membership_id,
            OrganizationMembership.organization_id == org_id,
        )
        .one_or_none()
    )
    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership record not found.")

    db.delete(membership)
    db.commit()
