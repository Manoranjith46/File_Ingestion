"""Background cleanup service for abandoned upload sessions and orphaned part files."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from config.redis_server import server as redis_server
from helpers.get_env import get_env

logger = logging.getLogger(__name__)

UPLOAD_ROOT = Path(get_env("UPLOAD_STORAGE_DIR", default=str(Path(__file__).resolve().parents[2] / "uploads"), required=False))
PARTS_ROOT = UPLOAD_ROOT / ".parts"
CLEANUP_INTERVAL_SECONDS = int(get_env("UPLOAD_CLEANUP_INTERVAL_SECONDS", default="300", required=False))

_cleanup_thread: threading.Thread | None = None
_cleanup_lock = threading.Lock()


def _meta_key(upload_id: str) -> str:
    return f"upload:{upload_id}:meta"


def _bitmap_key(upload_id: str) -> str:
    return f"upload:{upload_id}:bitmap"


def _chunk_hashes_key(upload_id: str) -> str:
    return f"upload:{upload_id}:chunk_hashes"


def purge_abandoned_upload_parts() -> list[str]:
    """
    Remove upload part directories whose Redis session has expired.

    Returns:
        list[str]: The list of upload IDs that were cleaned up.
    """
    cleaned_uploads: list[str] = []
    if not PARTS_ROOT.exists():
        return cleaned_uploads

    try:
        for parts_dir in PARTS_ROOT.iterdir():
            if not parts_dir.is_dir():
                continue
            upload_id = parts_dir.name
            try:
                if not redis_server.exists(_meta_key(upload_id)):
                    shutil.rmtree(parts_dir, ignore_errors=True)
                    redis_server.delete(_bitmap_key(upload_id), _chunk_hashes_key(upload_id))
                    cleaned_uploads.append(upload_id)
            except Exception as err:
                logger.error(f"Error purging abandoned upload session '{upload_id}': {err}")
    except Exception as err:
        logger.error(f"Error iterating parts storage directory: {err}")

    return cleaned_uploads


def _cleanup_loop() -> None:
    """
    Run the background cleanup loop until the application process exits.
    """
    while True:
        try:
            purged = purge_abandoned_upload_parts()
            if purged:
                logger.info(f"Cleaned up {len(purged)} abandoned upload session(s): {purged}")
        except Exception as err:
            logger.error(f"Unexpected error in cleanup loop iteration: {err}")
        time.sleep(CLEANUP_INTERVAL_SECONDS)


def start_cleanup_scheduler() -> threading.Thread:
    """
    Start the background cleanup scheduler daemon thread if not already running.

    Returns:
        threading.Thread: The daemon thread performing background cleanup.
    """
    global _cleanup_thread
    with _cleanup_lock:
        if _cleanup_thread is not None and _cleanup_thread.is_alive():
            return _cleanup_thread
        _cleanup_thread = threading.Thread(target=_cleanup_loop, name="upload-cleanup-scheduler", daemon=True)
        _cleanup_thread.start()
        return _cleanup_thread