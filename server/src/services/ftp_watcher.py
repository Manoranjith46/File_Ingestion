"""Background FTP dropzone watcher for server-to-server bulk file ingestion."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config.database import get_session_local
from helpers.get_env import get_env
from models.auth_model import User
from models.file_model import (
    Dataset,
    DatasetFolderFilesMapping,
    UploadedFile,
    UploadedFileSourceType,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration – mirrors the env-var pattern used by file_services.py
# ---------------------------------------------------------------------------

UPLOAD_ROOT = Path(
    get_env(
        "UPLOAD_STORAGE_DIR",
        default=str(Path(__file__).resolve().parents[2] / "uploads"),
        required=False,
    )
)

FTP_STORAGE_DIR_NAME = get_env("FTP_STORAGE_DIR", default="FTP", required=False)
FINAL_ROOT_NAME = get_env("FINAL_ROOT", default="files", required=False)
FTP_FINAL_ROOT = UPLOAD_ROOT / FINAL_ROOT_NAME / FTP_STORAGE_DIR_NAME

DROPZONE_DIR_NAME = get_env("FTP_DROPZONE_PATH", default="ftp_dropzone", required=False)
DROPZONE_ROOT = UPLOAD_ROOT / DROPZONE_DIR_NAME

POLL_INTERVAL_SECONDS = int(get_env("FTP_POLL_INTERVAL_SECONDS", default="10", required=False))
SAFETY_DELAY_SECONDS = int(get_env("FTP_SAFETY_DELAY_SECONDS", default="5", required=False))

# Extensions that indicate a file is still being written by the FTP client.
_INCOMPLETE_EXTENSIONS = frozenset({".filepart", ".tmp"})

# Singleton thread management (same pattern as cleanup_scheduler.py)
_watcher_thread: threading.Thread | None = None
_watcher_lock = threading.Lock()

# Default dataset name used for all FTP-ingested files.
_FTP_DATASET_NAME = "FTP Drops"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_file_still_writing(filepath: Path) -> bool:
    """Return True if the file's extension or mtime suggests it is incomplete."""
    if filepath.suffix.lower() in _INCOMPLETE_EXTENSIONS:
        return True
    try:
        mtime = filepath.stat().st_mtime
        if (time.time() - mtime) < SAFETY_DELAY_SECONDS:
            return True
    except OSError:
        # File may have been removed between listing and stat; treat as incomplete.
        return True
    return False


def _compute_sha256(filepath: Path) -> str:
    """Return the hex-encoded SHA-256 digest of *filepath*."""
    h = hashlib.sha256()
    with filepath.open("rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)  # 8 MiB read buffer
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _get_unique_filename(base_dir: Path, filename: str) -> Path:
    """Return a non-colliding path by appending (1), (2), etc. if needed."""
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        candidate = base_dir / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _get_or_create_ftp_dataset(db: Session, user_id: str) -> Dataset:
    """Return the user's 'FTP Drops' dataset, creating one if it does not exist."""
    dataset = (
        db.query(Dataset)
        .filter(
            Dataset.user_id == user_id,
            func.lower(Dataset.name) == _FTP_DATASET_NAME.lower(),
            Dataset.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if dataset is not None:
        return dataset

    dataset = Dataset(
        user_id=user_id,
        name=_FTP_DATASET_NAME,
        description="Auto-created dataset for files ingested via FTP.",
        status="Draft",
    )
    db.add(dataset)
    db.flush()
    return dataset


# ---------------------------------------------------------------------------
# Core per-file ingestion
# ---------------------------------------------------------------------------


def _ingest_ftp_file(username: str, filepath: Path) -> None:
    """Ingest a single completed FTP file for *username*.

    Opens its own database session so each file is an independent transaction.
    """
    db: Session = get_session_local()()
    try:
        # 1. Resolve user --------------------------------------------------
        user = (
            db.query(User)
            .filter(User.username == username)
            .one_or_none()
        )
        if user is None:
            logger.error(
                "FTP watcher: no user with username '%s' – skipping %s",
                username,
                filepath,
            )
            return

        # 2. Compute SHA-256 hash ------------------------------------------
        master_hash = _compute_sha256(filepath)
        file_size = filepath.stat().st_size
        original_name = filepath.name

        # 3. Deduplication check (zero I/O) --------------------------------
        existing_file = (
            db.query(UploadedFile)
            .filter(UploadedFile.master_hash == master_hash)
            .first()
        )

        if existing_file is not None:
            # Duplicate – reuse existing file_id, remove the dropzone copy.
            file_id = existing_file.id
            try:
                filepath.unlink()
            except OSError as err:
                logger.warning(
                    "FTP watcher: failed to delete duplicate dropzone file %s: %s",
                    filepath,
                    err,
                )
            logger.info(
                "FTP watcher: duplicate detected for '%s' (hash=%s) – reusing file_id=%s",
                original_name,
                master_hash,
                file_id,
            )
        else:
            # New file – move to final FTP storage directory.
            FTP_FINAL_ROOT.mkdir(parents=True, exist_ok=True)
            dest = _get_unique_filename(FTP_FINAL_ROOT, original_name)
            shutil.move(str(filepath), str(dest))

            new_file = UploadedFile(
                filename=original_name,
                file_size_bytes=file_size,
                master_hash=master_hash,
                physical_path=str(dest),
                source_type=UploadedFileSourceType.FTP,
            )
            db.add(new_file)
            db.flush()
            file_id = new_file.id
            logger.info(
                "FTP watcher: ingested new file '%s' → %s (file_id=%s)",
                original_name,
                dest,
                file_id,
            )

        # 4. Dataset & mapping ---------------------------------------------
        dataset = _get_or_create_ftp_dataset(db, user.id)

        # Check if mapping already exists to prevent duplicates when folder_id is None
        existing_mapping = (
            db.query(DatasetFolderFilesMapping)
            .filter(
                DatasetFolderFilesMapping.dataset_id == dataset.id,
                DatasetFolderFilesMapping.folder_id.is_(None),
                DatasetFolderFilesMapping.file_id == file_id,
                DatasetFolderFilesMapping.user_id == user.id,
            )
            .first()
        )

        if existing_mapping is None:
            stmt = (
                pg_insert(DatasetFolderFilesMapping)
                .values(
                    dataset_id=dataset.id,
                    folder_id=None,
                    file_id=file_id,
                    user_id=user.id,
                )
                .on_conflict_do_nothing(
                    constraint="uq_dataset_folder_file",
                )
            )
            db.execute(stmt)

        # Update dataset status to Draft when files are linked.
        if dataset.status == "Created":
            dataset.status = "Draft"

        db.commit()

    except Exception:
        db.rollback()
        logger.exception(
            "FTP watcher: unhandled error while ingesting %s for user '%s'",
            filepath,
            username,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scan cycle
# ---------------------------------------------------------------------------


def _scan_and_ingest() -> None:
    """Walk the FTP dropzone and ingest every completed file."""
    if not DROPZONE_ROOT.exists():
        return

    for user_dir in DROPZONE_ROOT.iterdir():
        if not user_dir.is_dir():
            continue

        username = user_dir.name

        for filepath in user_dir.iterdir():
            if not filepath.is_file():
                continue
            if _is_file_still_writing(filepath):
                continue

            try:
                _ingest_ftp_file(username, filepath)
            except Exception:
                logger.exception(
                    "FTP watcher: failed to process %s for user '%s'",
                    filepath,
                    username,
                )


# ---------------------------------------------------------------------------
# Background loop & public API
# ---------------------------------------------------------------------------


def _ftp_watch_loop() -> None:
    """Run the FTP watcher loop until the process exits."""
    logger.info(
        "FTP watcher started – polling %s every %ds (safety delay: %ds)",
        DROPZONE_ROOT,
        POLL_INTERVAL_SECONDS,
        SAFETY_DELAY_SECONDS,
    )
    while True:
        try:
            _scan_and_ingest()
        except Exception:
            logger.exception("FTP watcher: unexpected error in scan cycle")
        time.sleep(POLL_INTERVAL_SECONDS)


def start_ftp_watcher() -> threading.Thread:
    """Start the FTP dropzone watcher daemon thread if not already running.

    Returns:
        threading.Thread: The daemon thread performing background FTP ingestion.
    """
    global _watcher_thread
    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return _watcher_thread
        _watcher_thread = threading.Thread(
            target=_ftp_watch_loop,
            name="ftp-dropzone-watcher",
            daemon=True,
        )
        _watcher_thread.start()
        logger.info("FTP watcher daemon thread launched")
        return _watcher_thread
