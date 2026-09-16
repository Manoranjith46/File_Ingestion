"""Tenant context dependency and server-side organization authorization controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from config.database import get_db
from models.auth_model import Organization, OrganizationMembership, User
from services.auth_services import get_current_user
from utils.errors import UnauthorizedAccessError


@dataclass
class TenantContext:
    """Encapsulates the verified tenant and user context for the active request."""

    user: User
    organization: Organization | None
    role: str  # "super_admin" | "org_admin" | "member" | "none"

    @property
    def organization_id(self) -> str | None:
        """Return the active organization ID if present."""
        return self.organization.id if self.organization else None

    @property
    def is_super_admin(self) -> bool:
        """Return True if user has the global platform super_admin role."""
        return self.user.role == "super_admin"

    @property
    def is_org_admin(self) -> bool:
        """Return True if user is an administrator of the active organization."""
        return self.role == "org_admin"

    def require_organization(self) -> Organization:
        """Ensure an active organization context exists; raises 400/403 if absent."""
        if not self.organization:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Organization context required. Please select or specify an organization.",
            )
        if not self.organization.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The requested organization is inactive or disabled.",
            )
        return self.organization

    def require_org_admin(self) -> None:
        """Ensure user is an admin of the active organization (or raises 403)."""
        self.require_organization()
        if not self.is_org_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Organization admin privileges required.",
            )

    def require_super_admin(self) -> None:
        """Ensure user is a global Super Admin (or raises 403)."""
        if not self.is_super_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Platform Super Admin privileges required.",
            )


def get_tenant_context(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_organization_id: str | None = Header(default=None, alias="X-Organization-ID"),
    db: Session = Depends(get_db),
) -> TenantContext:
    """Resolve and cryptographically/relationally verify the tenant context for a request.

    Enforcement Rules:
    1. Authenticates user via Bearer token (Auth0 RS256 or HS256).
    2. Verifies user account is active.
    3. If X-Organization-ID is provided:
       - Checks database organization exists and is active.
       - Checks user's membership in that organization.
       - Super Admins CANNOT automatically access organization data without explicit membership.
    4. If X-Organization-ID is omitted:
       - Defaults to the user's active membership (if user belongs to exactly one or has an active membership).
       - If user has no memberships, organization is None.
    5. Returns verified TenantContext.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing access token",
        )

    access_token = authorization.removeprefix("Bearer ").strip()
    user = get_current_user(db, access_token)

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
        )

    # 1. If explicit organization requested via header
    if x_organization_id:
        clean_org_id = x_organization_id.strip()
        org = (
            db.query(Organization)
            .filter(
                (Organization.id == clean_org_id)
                | (Organization.auth0_org_id == clean_org_id)
            )
            .one_or_none()
        )
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Requested organization not found",
            )
        if not org.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization is inactive",
            )

        # Check explicit membership in requested organization
        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == org.id,
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.is_active == True,
            )
            .one_or_none()
        )

        # Super admin privilege escalation protection:
        # Super admin without explicit membership cannot access tenant data
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User does not have an active membership in the requested organization",
            )

        return TenantContext(
            user=user,
            organization=org,
            role=membership.role,
        )

    # 2. No explicit organization header supplied: find user's default active membership
    memberships = (
        db.query(OrganizationMembership)
        .join(Organization, OrganizationMembership.organization_id == Organization.id)
        .filter(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active == True,
            Organization.is_active == True,
        )
        .all()
    )

    if memberships:
        # Default to the first active membership
        active_membership = memberships[0]
        return TenantContext(
            user=user,
            organization=active_membership.organization,
            role=active_membership.role,
        )

    # User with no organization memberships (e.g. newly created user or standalone super admin)
    user_role = "super_admin" if user.role == "super_admin" else "none"
    return TenantContext(
        user=user,
        organization=None,
        role=user_role,
    )
