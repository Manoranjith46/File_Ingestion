import importlib
import sys
from pathlib import Path


def test_encrypt_str_falls_back_when_encryption_key_missing(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    crypto = importlib.import_module("helpers.crypto")
    importlib.reload(crypto)

    encrypted = crypto.encrypt_str("secret-token")

    assert encrypted
    assert crypto.decrypt_str(encrypted) == "secret-token"
