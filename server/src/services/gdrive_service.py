"""Background worker service for Google Drive cloud file ingestion."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import re
from fastapi import BackgroundTasks, HTTPException, status

from config.database import get_session_local
from config.redis_server import server as redis_server
from models.auth_model import User
from models.file_model import (
    AsyncIngestionJob,
    Dataset,
    DatasetFolderFilesMapping,
    Folder,
    UploadedFile,
    UploadedFileSourceType,
)
from schemas.file_schema import GDriveIngestResponse
from services.file_services import FINAL_ROOT, _uploaded_filename, _sync_dataset_status_from_mappings


GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."

GOOGLE_APPS_EXPORT_EXTENSIONS = {
    "application/vnd.google-apps.document": ".pdf",
    "application/vnd.google-apps.spreadsheet": ".pdf",
    "application/vnd.google-apps.presentation": ".pdf",
    "application/vnd.google-apps.drawing": ".pdf",
}


def is_google_apps_mime(mime_type: str | None) -> bool:
    """
    Check if a MIME type represents a native Google Workspace application.

    Args:
        mime_type (str | None): The MIME type string to evaluate.

    Returns:
        bool: True if native Google App, False otherwise.
    """
    if not mime_type:
        return False
    return mime_type.strip().startswith(GOOGLE_APPS_MIME_PREFIX)


def extract_gdrive_file_id(url_or_id: str) -> str:
    """
    Extract a Google Drive file ID from shared URLs or raw string identifiers.

    Args:
        url_or_id (str): The Google Drive URL or raw file ID string.

    Returns:
        str: The extracted Google Drive file ID.
    """
    clean = url_or_id.strip()
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", clean)
    if match:
        return match.group(1)
    match = re.search(r"id=([a-zA-Z0-9_-]+)", clean)
    if match:
        return match.group(1)
    if clean.startswith("http://") or clean.startswith("https://"):
        parts = clean.rstrip("/").split("/")
        return parts[-1]
    return clean


def initiate_gdrive_ingestion(
    db: Session,
    user: User,
    dataset_id: str,
    file_id_or_url: str,
    background_tasks: BackgroundTasks,
    folder_id: str | None = None,
    filename: str | None = None,
    mime_type: str | None = None,
    stream_chunks_generator = None,
) -> GDriveIngestResponse:
    """
    Initiate an asynchronous Google Drive ingestion job with Redis SETNX locks and native app support.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        dataset_id (str): Target dataset ID.
        file_id_or_url (str): Google Drive file ID or shared URL.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        folder_id (str | None): Optional virtual folder ID.
        filename (str | None): Optional override filename.
        mime_type (str | None): Optional MIME type of the file.
        stream_chunks_generator (iterable | None): Optional pre-configured byte stream generator.

    Returns:
        GDriveIngestResponse: Details of the queued ingestion job.

    Raises:
        HTTPException: If the target dataset is missing or a lock exists.
    """
    gdrive_file_id = extract_gdrive_file_id(file_id_or_url)
    if not gdrive_file_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Google Drive file ID or URL")

    # Verify dataset ownership
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.user_id == user.id, Dataset.is_deleted.is_(False)).one_or_none()
    if dataset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found or access denied")

    if folder_id:
        folder = db.query(Folder).filter(Folder.id == folder_id, Folder.user_id == user.id).one_or_none()
        if folder is None:
            folder_id = None

    is_native_app = is_google_apps_mime(mime_type)

    lock_key = f"lock:ingest:gdrive:{gdrive_file_id}"
    job_id = str(uuid4())

    # Redis SETNX Lock to prevent duplicate concurrent downloads of the same GDrive file
    lock_acquired = redis_server.set(lock_key, job_id, nx=True, ex=3600)
    if not lock_acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An ingestion job for this Google Drive file is already in progress.",
        )

    # Determine default filename; append .pdf for native apps if no extension provided
    target_filename = filename or f"gdrive_{gdrive_file_id}"
    if is_native_app and not Path(target_filename).suffix:
        ext = GOOGLE_APPS_EXPORT_EXTENSIONS.get(mime_type or "", ".pdf")
        target_filename = f"{target_filename}{ext}"

    job = AsyncIngestionJob(
        id=job_id,
        user_id=user.id,
        dataset_id=dataset_id,
        folder_id=folder_id,
        provider="GDrive",
        source_url_or_id=gdrive_file_id,
        filename=target_filename,
        status="pending",
        progress_percentage=0,
    )
    db.add(job)
    db.commit()

    if stream_chunks_generator is None:
        stream_chunks_generator = iter([])

    background_tasks.add_task(
        process_gdrive_ingestion_job,
        job_id=job.id,
        lock_key=lock_key,
        stream_chunks_generator=stream_chunks_generator,
        filename=target_filename,
        total_bytes=None,
        user_id=user.id,
        dataset_id=dataset_id,
        folder_id=folder_id,
        is_native_google_app=is_native_app,
        mime_type=mime_type,
    )

    return GDriveIngestResponse(
        job_id=job.id,
        status="pending",
        message="Google Drive ingestion job initiated" if not is_native_app else "Google Workspace export job initiated",
    )


def process_gdrive_ingestion_job(
    job_id: str,
    lock_key: str,
    stream_chunks_generator,
    filename: str,
    total_bytes: int | None,
    user_id: str,
    dataset_id: str,
    folder_id: str | None = None,
    is_native_google_app: bool = False,
    mime_type: str | None = None,
) -> None:
    """
    Background worker to stream Google Drive file/export content, perform real-time hashing, and atomically commit.

    Args:
        job_id (str): Unique AsyncIngestionJob identifier.
        lock_key (str): Redis distributed lock key to release upon completion.
        stream_chunks_generator (iterable): Generator yielding raw byte chunks.
        filename (str): Target filename for storage.
        total_bytes (int | None): Expected total byte size if known.
        user_id (str): Owner user ID.
        dataset_id (str): Target dataset ID.
        folder_id (str | None): Optional target folder ID.
        is_native_google_app (bool): Flag indicating if the file was exported from a Google Workspace App.
        mime_type (str | None): Optional original Google Workspace MIME type.
    """
    db: Session = get_session_local()()
    staging_path: Path | None = None
    progress_key = f"ingest:{job_id}:progress"

    try:
        # 1. Touch 1: Update job status to in_progress
        job = db.query(AsyncIngestionJob).filter(AsyncIngestionJob.id == job_id).one_or_none()
        if job:
            job.status = "in_progress"
            job.progress_percentage = 0
            db.commit()

        redis_server.set(progress_key, "0")

        # 2. Prepare staging file and incremental SHA-256 calculation
        staging_file_id = str(uuid4())
        staging_path = FINAL_ROOT / f"{staging_file_id}.pending"
        FINAL_ROOT.mkdir(parents=True, exist_ok=True)

        sha256_hash = hashlib.sha256()
        downloaded_bytes = 0

        with staging_path.open("wb") as destination:
            for chunk in stream_chunks_generator:
                if not chunk:
                    continue
                destination.write(chunk)
                sha256_hash.update(chunk)
                downloaded_bytes += len(chunk)

                if total_bytes and total_bytes > 0:
                    pct = min(99, int((downloaded_bytes / total_bytes) * 100))
                    redis_server.set(progress_key, str(pct))
                    if job:
                        job.progress_percentage = pct
                        db.commit()

            # Ensure hardware disk sync before renaming
            destination.flush()
            os.fsync(destination.fileno())

        master_hash = sha256_hash.hexdigest()

        # 3. Touch 2: Deduplication check and atomic DB finalize
        uploaded_file = (
            db.query(UploadedFile)
            .filter(UploadedFile.master_hash == master_hash)
            .first()
        )

        final_file_id: str
        if uploaded_file is not None:
            # File exists globally; re-use existing record and remove temp staging file
            final_file_id = uploaded_file.id
            if staging_path.exists():
                staging_path.unlink()
        else:
            # Save new physical file
            final_file_id = str(uuid4())
            final_filename = _uploaded_filename(filename)
            final_path = FINAL_ROOT / final_filename

            if final_path.exists():
                # Disambiguate filename collision
                final_path = FINAL_ROOT / f"{final_file_id}_{final_filename}"

            staging_path.replace(final_path)

            new_uploaded_file = UploadedFile(
                id=final_file_id,
                filename=filename,
                file_size_bytes=downloaded_bytes,
                master_hash=master_hash,
                physical_path=str(final_path),
                source_type=UploadedFileSourceType.GDrive,
            )
            db.add(new_uploaded_file)
            db.flush()

        # Insert dataset-folder file mapping
        stmt = (
            pg_insert(DatasetFolderFilesMapping)
            .values(
                dataset_id=dataset_id,
                folder_id=folder_id,
                file_id=final_file_id,
                user_id=user_id,
            )
            .on_conflict_do_nothing(
                constraint="uq_dataset_folder_file",
            )
        )
        db.execute(stmt)

        # Update Job Ledger status to completed
        job = db.query(AsyncIngestionJob).filter(AsyncIngestionJob.id == job_id).one_or_none()
        if job:
            job.status = "completed"
            job.progress_percentage = 100
            job.master_hash = master_hash
            job.file_id = final_file_id
            job.error_message = None

        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one_or_none()
        if dataset:
            _sync_dataset_status_from_mappings(db, dataset)

        db.commit()
        redis_server.set(progress_key, "100")

    except Exception as e:
        db.rollback()
        if staging_path and staging_path.exists():
            staging_path.unlink(missing_ok=True)

        job = db.query(AsyncIngestionJob).filter(AsyncIngestionJob.id == job_id).one_or_none()
        if job:
            job.status = "failed"
            job.progress_percentage = 0
            job.error_message = str(e)
            db.commit()

    finally:
        db.close()
        redis_server.delete(lock_key)
        redis_server.delete(progress_key)
