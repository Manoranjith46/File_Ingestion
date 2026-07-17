"""Background cleanup for abandoned upload sessions and part files."""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from config.redis_server import server as redis_server
from helpers.get_env import get_env

UPLOAD_ROOT = Path(get_env("UPLOAD_STORAGE_DIR", default=str(Path(__file__).resolve().parents[2] / "uploads"), required=False))
PARTS_ROOT = UPLOAD_ROOT / ".parts"
CLEANUP_INTERVAL_SECONDS = int(get_env("UPLOAD_CLEANUP_INTERVAL_SECONDS", default="300", required=False))

_cleanup_thread: threading.Thread | None = None
_cleanup_lock = threading.Lock()


def _meta_key(upload_id: str) -> str:
    return f"upload:{upload_id}:meta"


def purge_abandoned_upload_parts() -> list[str]:
    """Remove upload part directories whose Redis session has expired.

    Returns:
        list[str]: The upload IDs that were cleaned up.
    """
    cleaned_uploads: list[str] = []
    if not PARTS_ROOT.exists():
        return cleaned_uploads

    for parts_dir in PARTS_ROOT.iterdir():
        if not parts_dir.is_dir():
            continue
        upload_id = parts_dir.name
        if not redis_server.exists(_meta_key(upload_id)):
            shutil.rmtree(parts_dir, ignore_errors=True)
            cleaned_uploads.append(upload_id)
    return cleaned_uploads


def _cleanup_loop() -> None:
    """Run the cleanup loop until the process exits."""
    while True:
        try:
            purge_abandoned_upload_parts()
        except Exception:
            pass
        time.sleep(CLEANUP_INTERVAL_SECONDS)


def start_cleanup_scheduler() -> threading.Thread:
    """Start the background cleanup scheduler once.

    Returns:
        threading.Thread: The daemon thread that performs cleanup.
    """
    global _cleanup_thread
    with _cleanup_lock:
        if _cleanup_thread is not None and _cleanup_thread.is_alive():
            return _cleanup_thread
        _cleanup_thread = threading.Thread(target=_cleanup_loop, name="upload-cleanup-scheduler", daemon=True)
        _cleanup_thread.start()
        return _cleanup_thread