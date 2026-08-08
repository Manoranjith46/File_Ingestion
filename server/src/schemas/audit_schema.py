"""Schema definitions for audit logging endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    """Response model for a single audit log entry."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    request_id: str
    user_id: str | None = None
    username: str | None = None
    method: str
    status: int = Field(alias="status_code")
    timestamp: datetime = Field(alias="created_at")
    action: str


class AuditLogCount(BaseModel):
    """Aggregate counts for a user's audit activity."""

    total: int
    success: int
    failed: int


class AuditLogListResponse(BaseModel):
    """Paginated response wrapper for audit logs."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AuditLogResponse]
    count: AuditLogCount
    sort: str | None = None
    limit: int


class AuditWorkerHealthResponse(BaseModel):
    """Health response for the audit worker service."""

    status: str
    message: str
