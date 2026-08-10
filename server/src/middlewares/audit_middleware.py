"""Middleware that publishes audit events to the dedicated Redis stream."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from config.database import get_session_local
from config.redis_server import audit_stream_name, server as redis_server
from services.auth_services import get_current_user
from utils.actions import get_action_name, is_valid_audit_action

logger = logging.getLogger(__name__)


class AuditMiddleware(BaseHTTPMiddleware):
    """Capture request-level metadata and publish it to the audit stream."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        start_time = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            if _should_skip_request(request):
                if response is not None:
                    return response
                return

            duration_ms = round((time.perf_counter() - start_time) * 1000, 3)
            status_code = response.status_code if response is not None else 500
            user_id = await _resolve_request_user_id(request)
            action = getattr(request.state, "audit_action", None) or get_action_name(request.method, request.url.path)
            if not is_valid_audit_action(action):
                if response is not None:
                    return response
                return

            payload = {
                "request_id": request_id,
                "user_id": user_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "ip_address": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "action": action,
            }
            try:
                asyncio.create_task(
                    _publish_audit_payload(payload)
                )
            except Exception:
                pass


def _should_skip_request(request: Request) -> bool:
    """Return True for requests that should not be tracked as audit events."""
    if request.method.upper() == "OPTIONS":
        return True

    if getattr(request.state, "skip_audit", False):
        return True

    path = request.url.path.rstrip("/")
    return path in {"/v1/audit-logs", "/audit-logs", "/health/audit-worker"}


async def _resolve_request_user_id(request: Request) -> str | None:
    """Best-effort resolution of the authenticated user ID from the request."""
    if getattr(getattr(request, "state", None), "user", None) is not None:
        return getattr(request.state.user, "id", None)
    if getattr(getattr(request, "state", None), "user_id", None) is not None:
        return request.state.user_id

    authorization = request.headers.get("authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None

    return await asyncio.to_thread(_resolve_user_id_from_token, token)


def _resolve_user_id_from_token(token: str) -> str | None:
    """Resolve the current user ID from a bearer token using a short-lived DB session."""
    db = get_session_local()()
    try:
        user = get_current_user(db, token)
        return getattr(user, "id", None)
    except Exception:
        return None
    finally:
        db.close()


async def _publish_audit_payload(payload: dict) -> None:
    """Publish a single payload to the dedicated Redis audit stream without blocking the event loop."""
    try:
        await asyncio.to_thread(
            redis_server.xadd,
            audit_stream_name,
            {"payload": json.dumps(payload)},
        )
    except Exception:
        logger.debug("Audit payload publish failed", exc_info=True)
