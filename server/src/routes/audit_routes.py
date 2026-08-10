"""Routes for inspecting audit logs and worker health."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from config.database import get_db
from models.auth_model import User
from schemas.audit_schema import AuditLogCount, AuditLogListResponse, AuditWorkerHealthResponse, AuditLogResponse
from services.audit_worker import get_audit_worker_health
from services.auth_services import get_current_user
from models.audit_model import AuditLog
audit_router = APIRouter()


@audit_router.get("/v1/audit-logs", response_model=AuditLogListResponse)
def list_audit_logs(
    sort: str | None = Query(
        default=None,
        description="Sort by field: timestamp, status, user_id, username, or method",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    date_range: str | None = Query(
        default=None,
        description="Optional date range filter: Today, Yesterday, Last 7 Days, Last 30 Days",
    ),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Optional status filter: success or failed",
    ),
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Return the current user's audit logs for the dashboard."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")

    access_token = authorization.removeprefix("Bearer ").strip()
    user = get_current_user(db, access_token)

    def _apply_date_range(query):
        if date_range is None:
            return query

        now = datetime.utcnow()
        today = datetime(now.year, now.month, now.day)
        if date_range == "Today":
            start = today
            end = today + timedelta(days=1)
        elif date_range == "Yesterday":
            start = today - timedelta(days=1)
            end = today
        elif date_range == "Last 7 Days":
            start = today - timedelta(days=7)
            end = now
        elif date_range == "Last 30 Days":
            start = today - timedelta(days=30)
            end = now
        else:
            return query

        return query.filter(AuditLog.created_at >= start, AuditLog.created_at < end)

    def _apply_status_filter(query):
        if status_filter is None:
            return query
        normalized = status_filter.strip().lower()
        if normalized == "success":
            return query.filter(AuditLog.status_code < 400)
        if normalized == "failed":
            return query.filter(AuditLog.status_code >= 400)
        return query

    total_count = db.query(func.count()).select_from(AuditLog).filter(AuditLog.user_id == user.id).scalar() or 0
    success_count = (
        db.query(func.count())
        .select_from(AuditLog)
        .filter(AuditLog.user_id == user.id, AuditLog.status_code < 400)
        .scalar()
        or 0
    )
    failed_count = (
        db.query(func.count())
        .select_from(AuditLog)
        .filter(AuditLog.user_id == user.id, AuditLog.status_code >= 400)
        .scalar()
        or 0
    )

    results_query = (
        db.query(AuditLog, User.username)
        .outerjoin(User, AuditLog.user_id == User.id)
        .filter(AuditLog.user_id == user.id)
    )

    results_query = _apply_date_range(results_query)
    results_query = _apply_status_filter(results_query)

    sort_field = (sort or "timestamp").strip().lower() if sort else "timestamp"
    if sort_field in {"timestamp", "created_at"}:
        results_query = results_query.order_by(AuditLog.created_at.desc())
    elif sort_field == "status":
        results_query = results_query.order_by(AuditLog.status_code.desc())
    elif sort_field == "user_id":
        results_query = results_query.order_by(AuditLog.user_id.desc())
    elif sort_field == "username":
        results_query = results_query.order_by(User.username.desc())
    elif sort_field == "method":
        results_query = results_query.order_by(AuditLog.method.desc())
    else:
        results_query = results_query.order_by(AuditLog.created_at.desc())

    rows = results_query.limit(limit).all()
    items = [
        AuditLogResponse(
            id=log.id,
            request_id=log.request_id,
            user_id=log.user_id,
            username=username,
            method=log.method,
            status_code=log.status_code,
            created_at=log.created_at,
            action=log.action or "",
        )
        for log, username in rows
    ]

    return AuditLogListResponse(
        items=items,
        count=AuditLogCount(total=total_count, success=success_count, failed=failed_count),
        sort=sort,
        limit=limit,
    )


@audit_router.get("/health/audit-worker", response_model=AuditWorkerHealthResponse)
def audit_worker_health() -> AuditWorkerHealthResponse:
    """Return a lightweight health payload for the audit worker."""
    return AuditWorkerHealthResponse(**get_audit_worker_health())
