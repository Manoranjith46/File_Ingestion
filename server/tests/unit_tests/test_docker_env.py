"""Regression tests for container-friendly environment loading."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import src.helpers.get_env as get_env_module


def test_load_environment_variables_uses_existing_environment_when_dotenv_is_missing(monkeypatch, tmp_path: Path) -> None:
    """Environment variables passed to the container should work even if .env is absent."""
    missing_env = tmp_path / ".env"
    monkeypatch.setattr(get_env_module, "ENV_PATH", missing_env)
    monkeypatch.delenv("Connection_String", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    monkeypatch.setenv("Connection_String", "postgresql+psycopg2://postgres:secret@postgres:5432/app")
    monkeypatch.setenv("JWT_SECRET_KEY", "docker-secret")

    get_env_module.load_environment_variables()

    assert os.getenv("Connection_String") == "postgresql+psycopg2://postgres:secret@postgres:5432/app"
    assert get_env_module.get_env("JWT_SECRET_KEY", required=True) == "docker-secret"
