"""Routes for inspecting audit logs and worker health."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from config.database import get_db
from schemas.audit_schema import AuditLogListResponse, AuditWorkerHealthResponse
from services.audit_worker import get_audit_worker_health
from services.auth_services import get_current_user
from models.audit_model import AuditLog

audit_router = APIRouter()


@audit_router.get("/v1/audit-logs", response_model=AuditLogListResponse)
def list_audit_logs(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Return the current user's audit logs for the dashboard."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")

    access_token = authorization.removeprefix("Bearer ").strip()
    user = get_current_user(db, access_token)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user.id)
        .order_by(AuditLog.created_at.desc())
        .limit(50)
        .all()
    )
    return AuditLogListResponse(items=rows, count=len(rows))


@audit_router.get("/health/audit-worker", response_model=AuditWorkerHealthResponse)
def audit_worker_health() -> AuditWorkerHealthResponse:
    """Return a lightweight health payload for the audit worker."""
    return AuditWorkerHealthResponse(**get_audit_worker_health())
