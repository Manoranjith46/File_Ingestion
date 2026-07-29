"""Unit tests for upload cleanup utilities."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SERVER_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from src.services import cleanup_scheduler


class FakeRedis:
    """Minimal Redis stub for cleanup tests."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    def exists(self, key: str) -> bool:
        return key in self.keys

    def delete(self, *keys: str) -> None:
        self.keys.difference_update(keys)


def test_purge_abandoned_upload_parts_removes_expired_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired upload part directories should be purged when the session metadata is gone."""
    parts_root = tmp_path / ".parts"
    parts_root.mkdir(parents=True, exist_ok=True)
    upload_dir = parts_root / "expired-upload"
    upload_dir.mkdir(parents=True, exist_ok=True)

    fake_redis = FakeRedis()
    monkeypatch.setattr(cleanup_scheduler, "PARTS_ROOT", parts_root)
    monkeypatch.setattr(cleanup_scheduler, "redis_server", fake_redis)

    cleaned_uploads = cleanup_scheduler.purge_abandoned_upload_parts()

    assert cleaned_uploads == ["expired-upload"]
    assert not upload_dir.exists()


def test_start_cleanup_scheduler_returns_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduler should create and return a daemon thread."""
    monkeypatch.setattr(cleanup_scheduler, "_cleanup_loop", lambda: None)
    cleanup_scheduler._cleanup_thread = None

    thread = cleanup_scheduler.start_cleanup_scheduler()

    assert thread is not None
    assert thread.daemon is True
