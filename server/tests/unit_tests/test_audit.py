from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret")

from models.auth_model import Base  # noqa: E402
from models.audit_model import AuditLog  # noqa: E402
from services.audit_worker import persist_audit_logs  # noqa: E402
from utils.actions import get_action_name, is_valid_audit_action  # noqa: E402


def test_persist_audit_logs_is_idempotent_for_duplicate_request_ids():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)

    try:
        payloads = [
            {
                "request_id": "req-123",
                "user_id": "user-1",
                "method": "GET",
                "path": "/v1/health",
                "status_code": 200,
                "duration_ms": 12.5,
                "ip_address": "127.0.0.1",
                "user_agent": "pytest",
                "action": "Login",
            }

        ]

        inserted = persist_audit_logs(session, payloads)
        assert inserted == 1

        duplicate_inserted = persist_audit_logs(session, payloads)
        assert duplicate_inserted == 0

        stored = session.query(AuditLog).filter(AuditLog.request_id == "req-123").all()
        assert len(stored) == 1
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_get_action_name_filters_to_valid_audit_actions():
    assert get_action_name("POST", "/auth/login") == "Login"
    assert is_valid_audit_action(get_action_name("POST", "/auth/login")) is True

    assert get_action_name("POST", "/v1/upload/init") is None
    assert is_valid_audit_action(get_action_name("POST", "/v1/upload/init")) is False

    assert get_action_name("PATCH", "/v1/datasets/1234") == "Dataset Updated"
    assert is_valid_audit_action(get_action_name("PATCH", "/v1/datasets/1234")) is True
