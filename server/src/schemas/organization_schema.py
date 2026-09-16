"""Pydantic schemas for multi-organization management and Google SSO configuration."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class OrganizationCreateRequest(BaseModel):
    """Payload for creating a new organization."""

    name: str = Field(..., min_length=2, max_length=255, description="Unique slug or identifier name for the organization")
    display_name: str | None = Field(None, max_length=255, description="Human-friendly organization display name")
    auth0_org_id: str | None = Field(None, max_length=128, description="Optional associated Auth0 organization ID (org_xxx)")
    google_sso_enabled: bool = Field(default=False, description="Whether Google SSO connection is enabled for this organization")

    # Optional initial org admin credentials to auto-provision during creation
    admin_email: EmailStr | None = Field(None, description="Email for initial Organization Admin")
    admin_name: str | None = Field(None, max_length=255, description="Full name for initial Organization Admin")
    admin_username: str | None = Field(None, min_length=3, max_length=64, description="Username for initial Organization Admin")
    admin_password: str | None = Field(None, min_length=8, max_length=128, description="Password for initial Organization Admin")


class OrganizationUpdateRequest(BaseModel):
    """Payload for modifying organization properties (Super Admin)."""

    display_name: str | None = Field(None, max_length=255)
    auth0_org_id: str | None = Field(None, max_length=128)
    is_active: bool | None = Field(None, description="Activate or suspend organization")
    google_sso_enabled: bool | None = Field(None, description="Toggle Google SSO connection enablement")


class OrgSettingsUpdateRequest(BaseModel):
    """Payload for modifying organization settings (Org Admin self-serve)."""

    display_name: str | None = Field(None, max_length=255)
    google_sso_enabled: bool | None = Field(None, description="Enable or disable Google SSO requirement")


class OrganizationResponse(BaseModel):
    """Public representation of an organization."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    display_name: str | None = None
    auth0_org_id: str | None = None
    is_active: bool
    google_sso_enabled: bool
    created_at: datetime
    updated_at: datetime


class OrganizationMemberResponse(BaseModel):
    """Public representation of a member in an organization."""

    model_config = ConfigDict(from_attributes=True)

    membership_id: str
    user_id: str
    email: str
    username: str | None = None
    full_name: str | None = None
    role: str  # "org_admin" | "member"
    is_active: bool
    created_at: datetime


class AddMemberRequest(BaseModel):
    """Payload to add or invite a user to an organization."""

    user_identifier: str | None = Field(None, description="User ID, username, or email of an existing user")
    email: EmailStr | None = Field(None, description="Email of user to add or invite")
    role: str = Field(default="member", description="Role to assign in organization: 'org_admin' | 'member'")
    user_name: str | None = Field(None, max_length=255, description="Full name if provisioning a new user")
    temporary_password: str | None = Field(None, min_length=8, max_length=128, description="Password if provisioning a new user")


class UpdateMemberRoleRequest(BaseModel):
    """Payload to modify a member's role in an organization."""

    role: str = Field(..., description="New role in organization: 'org_admin' | 'member'")
