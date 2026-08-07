import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_resolve_request_user_id_uses_bearer_header(monkeypatch):
    audit_middleware = importlib.import_module("middlewares.audit_middleware")
    importlib.reload(audit_middleware)

    class DummyUser:
        id = "user-123"

    captured = {}

    def fake_get_current_user(db, token):
        captured["token"] = token
        return DummyUser()

    class DummySession:
        def close(self):
            return None

    monkeypatch.setattr(audit_middleware, "get_current_user", fake_get_current_user)
    monkeypatch.setattr(audit_middleware, "get_session_local", lambda: lambda: DummySession())

    request = SimpleNamespace(headers={"authorization": "Bearer abc123"}, state=SimpleNamespace())

    user_id = asyncio.run(audit_middleware._resolve_request_user_id(request))

    assert user_id == "user-123"
    assert captured["token"] == "abc123"
