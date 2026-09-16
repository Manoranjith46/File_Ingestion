"""HTTP routes for Organization Management and Google SSO Configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from config.database import get_db
from middlewares.tenant_context import TenantContext, get_tenant_context
from schemas.organization_schema import (
    AddMemberRequest,
    OrganizationCreateRequest,
    OrganizationMemberResponse,
    OrganizationResponse,
    OrganizationUpdateRequest,
    OrgSettingsUpdateRequest,
    UpdateMemberRoleRequest,
)
from services.organization_service import (
    add_organization_member,
    create_organization,
    get_organization_by_id,
    list_organization_members,
    list_organizations,
    remove_organization_member,
    update_member_role,
    update_org_settings_self_serve,
    update_organization,
)

organization_router = APIRouter(prefix="/v1/organizations", tags=["Organizations"])


# ===========================================================================
# 1. Platform Super Admin Endpoints
# ===========================================================================

@organization_router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new organization (Super Admin only)",
)
def create_organization_endpoint(
    payload: OrganizationCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Provision a new tenant organization. Requires Platform Super Admin privileges.
    """
    tenant.require_super_admin()
    return create_organization(db, payload)


@organization_router.get(
    "",
    response_model=list[OrganizationResponse],
    summary="List all organizations (Super Admin only)",
)
def list_organizations_endpoint(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    include_inactive: bool = Query(default=True),
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    List all organizations across the platform. Requires Platform Super Admin privileges.
    """
    tenant.require_super_admin()
    return list_organizations(db, page=page, limit=limit, include_inactive=include_inactive)


@organization_router.get(
    "/{org_id}",
    response_model=OrganizationResponse,
    summary="Get organization details (Super Admin only)",
)
def get_organization_endpoint(
    org_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Retrieve details of an organization by ID or Auth0 Org ID. Requires Super Admin.
    """
    tenant.require_super_admin()
    return get_organization_by_id(db, org_id)


@organization_router.patch(
    "/{org_id}",
    response_model=OrganizationResponse,
    summary="Update organization / suspend / toggle Google SSO (Super Admin only)",
)
def update_organization_endpoint(
    org_id: str,
    payload: OrganizationUpdateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Modify an organization's properties, suspend or activate it, or configure Auth0 sync.
    Requires Platform Super Admin privileges.
    """
    tenant.require_super_admin()
    return update_organization(db, org_id, payload)


# ===========================================================================
# 2. Organization Admin Self-Serve Endpoints (/me)
# ===========================================================================

@organization_router.get(
    "/me/profile",
    response_model=OrganizationResponse,
    summary="Get current active organization profile (Org Admin or Member)",
)
def get_my_organization_endpoint(
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Retrieve details of the organization active in the current request context.
    """
    org = tenant.require_organization()
    return org


@organization_router.patch(
    "/me/settings",
    response_model=OrganizationResponse,
    summary="Update current organization settings and toggle Google SSO (Org Admin)",
)
def update_my_organization_settings(
    payload: OrgSettingsUpdateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Update settings for the active organization, including enabling/disabling Google SSO.
    Requires Organization Admin privileges in the active organization.
    """
    tenant.require_org_admin()
    org = tenant.require_organization()
    return update_org_settings_self_serve(db, org, payload)


@organization_router.get(
    "/me/members",
    response_model=list[OrganizationMemberResponse],
    summary="List members of current active organization (Org Admin)",
)
def list_my_organization_members(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    List all members affiliated with the current active organization.
    """
    tenant.require_org_admin()
    org = tenant.require_organization()
    return list_organization_members(db, org.id)


@organization_router.post(
    "/me/members",
    response_model=OrganizationMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member to the current organization (Org Admin)",
)
def add_my_organization_member(
    payload: AddMemberRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Add a user to the active organization as either 'org_admin' or 'member'.
    Requires Organization Admin privileges.
    """
    tenant.require_org_admin()
    org = tenant.require_organization()
    return add_organization_member(db, org.id, payload)


@organization_router.patch(
    "/me/members/{membership_id}",
    response_model=OrganizationMemberResponse,
    summary="Update a member's role in the organization (Org Admin)",
)
def update_my_organization_member_role(
    membership_id: str,
    payload: UpdateMemberRoleRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Change a member's role ('org_admin' vs 'member') within the active organization.
    Requires Organization Admin privileges.
    """
    tenant.require_org_admin()
    org = tenant.require_organization()
    return update_member_role(db, org.id, membership_id, payload)


@organization_router.delete(
    "/me/members/{membership_id}",
    status_code=status.HTTP_200_OK,
    summary="Remove a member from the organization (Org Admin)",
)
def remove_my_organization_member(
    membership_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Remove a member's affiliation from the active organization.
    Requires Organization Admin privileges.
    """
    tenant.require_org_admin()
    org = tenant.require_organization()
    remove_organization_member(db, org.id, membership_id)
    return {"status": "removed", "membership_id": membership_id}
