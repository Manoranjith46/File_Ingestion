from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret")

from config.database import get_db  # noqa: E402
from main import app  # noqa: E402
from models.auth_model import Base  # noqa: E402


@pytest.fixture()
def client():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_audit_health_endpoint(client: TestClient):
    response = client.get("/health/audit-worker")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_audit_logs_endpoint_requires_auth(client: TestClient):
    response = client.get("/v1/audit-logs")
    assert response.status_code == 401
