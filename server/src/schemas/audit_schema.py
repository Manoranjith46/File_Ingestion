"""Schema definitions for audit logging endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogResponse(BaseModel):
    """Response model for a single audit log entry."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    request_id: str
    user_id: str | None = None
    method: str
    path: str
    status_code: int
    duration_ms: float
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    """Paginated response wrapper for audit logs."""

    items: list[AuditLogResponse]
    count: int


class AuditWorkerHealthResponse(BaseModel):
    """Health response for the audit worker service."""

    status: str
    message: str
