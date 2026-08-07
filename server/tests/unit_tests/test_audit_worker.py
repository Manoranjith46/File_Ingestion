import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_decode_payload_accepts_string_values_from_redis():
    audit_worker = importlib.import_module("services.audit_worker")
    importlib.reload(audit_worker)

    payload = audit_worker._decode_message_payload({"payload": '{"request_id": "req-1", "user_id": "user-1"}'})

    assert payload["request_id"] == "req-1"
    assert payload["user_id"] == "user-1"
