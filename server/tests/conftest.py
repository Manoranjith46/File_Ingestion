from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

SERVER_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = SERVER_ROOT / "src"
for path in (str(SERVER_ROOT), str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("Connection_String", "sqlite:///:memory:")
os.environ.setdefault("JWT_SECRET_KEY", "test-access-secret")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "test-refresh-secret")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_DAYS", "7")
os.environ.setdefault("OTP_EXPIRE_MINUTES", "10")
os.environ.setdefault("PASSWORD_RESET_EXPIRE_MINUTES", "15")
os.environ.setdefault("MAX_ACTIVE_SESSIONS", "3")
os.environ.setdefault("ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")

from models.auth_model import Base
from routes import auth_routes, file_routes, ftp_routes
from main import app as fastapi_app
import main as main_module
import config.database as config_database


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Provide a FastAPI TestClient wired to an isolated in-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db() -> Iterator[Session]:
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(main_module, "redis_server_status", lambda: None)
    monkeypatch.setattr(main_module, "Check_db_Connection", lambda: None)
    monkeypatch.setattr(main_module, "start_cleanup_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "start_ftp_watcher", lambda: None)

    async def _noop_audit_worker() -> None:
        return None

    monkeypatch.setattr(main_module, "start_audit_worker", _noop_audit_worker)
    monkeypatch.setattr(main_module, "get_engine", lambda: engine)
    monkeypatch.setattr(config_database, "get_engine", lambda: engine)
    monkeypatch.setattr(config_database, "get_session_local", lambda: sessionmaker(autocommit=False, autoflush=False, bind=engine))

    class _MockRedis:
        def __init__(self):
            self._data = {}
            self._hashes = {}
            self._bitmaps = {}
            self._sets = {}
            self._counters = {}
            self._ttls = {}

        def register_script(self, script):
            return lambda *args, **kwargs: 1

        def ping(self):
            return True

        def hset(self, name, key=None, value=None, mapping=None):
            if mapping is not None:
                self._hashes[name] = {**self._hashes.get(name, {}), **mapping}
                return len(mapping)
            if key is None:
                raise TypeError("hset() missing required argument 'key' (or provide mapping)")
            if name not in self._hashes:
                self._hashes[name] = {}
            self._hashes[name][key] = value
            return 1

        def hgetall(self, name):
            return self._hashes.get(name, {})

        def expire(self, name, ttl):
            return True

        def delete(self, *names):
            for name in names:
                self._hashes.pop(name, None)
                self._bitmaps.pop(name, None)
                self._data.pop(name, None)
                self._sets.pop(name, None)
            return len(names)

        def exists(self, name):
            return int(name in self._hashes or name in self._bitmaps or name in self._data or name in self._sets)

        def bitcount(self, name):
            bitmap = self._bitmaps.get(name, {})
            return sum(1 for v in bitmap.values() if v == 1)

        def setbit(self, name, offset, value):
            self._bitmaps.setdefault(name, {})[int(offset)] = int(value)
            return 0

        def getbit(self, name, offset):
            return self._bitmaps.get(name, {}).get(int(offset), 0)

        def zscore(self, key, member):
            return None

        def zrem(self, key, member):
            return 0

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self._data:
                return False
            self._data[key] = value
            if ex:
                self._ttls[key] = ex
            return True

        def get(self, key):
            return self._data.get(key)

        def incr(self, key):
            self._counters[key] = self._counters.get(key, 0) + 1
            return self._counters[key]

        def decr(self, key):
            val = self._counters.get(key, 0) - 1
            self._counters[key] = max(0, val)
            return self._counters[key]


        def ttl(self, key):
            return self._ttls.get(key, -1)

        def xadd(self, *args, **kwargs):
            return "0-0"

        def xreadgroup(self, *args, **kwargs):
            return []

        def xack(self, *args, **kwargs):
            return 0

        def xgroup_create(self, *args, **kwargs):
            return True

    mock_redis = _MockRedis()
    import config.redis_server as redis_server_module
    import services.auth_services as auth_services_module
    import services.file_services as file_services_module
    import services.ext_ftp_service as ext_ftp_service_module

    monkeypatch.setattr(redis_server_module, "server", mock_redis)
    monkeypatch.setattr(redis_server_module, "active_session_limiter", lambda *args, **kwargs: 1)
    monkeypatch.setattr(redis_server_module, "atomic_chunk_state", lambda *args, **kwargs: 0)

    monkeypatch.setattr(auth_services_module, "redis_server", mock_redis)
    monkeypatch.setattr(auth_services_module, "active_session_limiter", lambda *args, **kwargs: 1)

    monkeypatch.setattr(file_services_module, "redis_server", mock_redis)
    monkeypatch.setattr(file_services_module, "atomic_chunk_state", lambda *args, **kwargs: 0)

    monkeypatch.setattr(ext_ftp_service_module, "redis_server", mock_redis)

    original_overrides = dict(fastapi_app.dependency_overrides)
    fastapi_app.dependency_overrides[auth_routes.get_db] = override_get_db
    fastapi_app.dependency_overrides[file_routes.get_db] = override_get_db
    fastapi_app.dependency_overrides[ftp_routes.get_db] = override_get_db

    with TestClient(fastapi_app) as test_client:
        yield test_client

    fastapi_app.dependency_overrides.clear()
    fastapi_app.dependency_overrides.update(original_overrides)
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
