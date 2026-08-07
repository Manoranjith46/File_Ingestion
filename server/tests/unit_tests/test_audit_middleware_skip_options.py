import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_skip_options_requests_from_audit_log():
    audit_middleware = importlib.import_module("middlewares.audit_middleware")
    importlib.reload(audit_middleware)

    request = SimpleNamespace(method="OPTIONS", url=SimpleNamespace(path="/v1/datasets"), headers={}, state=SimpleNamespace())

    assert audit_middleware._should_skip_request(request)
