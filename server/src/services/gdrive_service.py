"""Background worker service for Google Drive cloud file ingestion."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import re
import requests as http_requests
from google.oauth2.credentials import Credentials
from fastapi import BackgroundTasks

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
from services.file_services import FINAL_ROOT, GDRIVE_STORAGE_DIR, _uploaded_filename, _get_unique_filename, _sync_dataset_status_from_mappings
from helpers.get_env import get_env
from helpers.crypto import decrypt_str
from utils.errors import IntegrationConnectionError, IntegrationRequestError, DatasetNotFoundError, InvalidRelativePathError

logger = logging.getLogger(__name__)

GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."

GOOGLE_APPS_EXPORT_EXTENSIONS = {
    "application/vnd.google-apps.document": ".pdf",
    "application/vnd.google-apps.spreadsheet": ".pdf",
    "application/vnd.google-apps.presentation": ".pdf",
    "application/vnd.google-apps.drawing": ".pdf",
}

GOOGLE_APPS_EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.drawing": "application/pdf",
}

GDRIVE_FILE_META_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"
GDRIVE_FILE_DOWNLOAD_URL = "https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
GDRIVE_FILE_EXPORT_URL = "https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType={export_mime}"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
STREAM_CHUNK_SIZE = 8192


def _build_google_credentials(refresh_token: str) -> Credentials:
    """Build Google OAuth2 credentials from a stored refresh token."""
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=get_env("GOOGLE_CLIENT_ID", default="", required=False).strip(),
        client_secret=get_env("GOOGLE_CLIENT_SECRET", default="", required=False).strip(),
    )


def get_user_google_access_token(user: User) -> str:
    """Obtain a fresh Google OAuth2 access token for the authenticated user."""
    if not user.google_refresh_token:
        raise IntegrationConnectionError(message="Google account not connected. Please connect your Google account first.")
    refresh_token = decrypt_str(user.google_refresh_token)
    creds = _build_google_credentials(refresh_token)
    from google.auth.transport.requests import Request as GoogleAuthRequest
    creds.refresh(GoogleAuthRequest())
    if not creds.token:
        raise IntegrationRequestError(message="Failed to obtain Google access token.")
    return creds.token


def _stream_gdrive_download(gdrive_file_id: str, refresh_token: str, mime_type: str | None = None):
    """
    Stream-download a Google Drive file using the user's refresh token.

    Yields raw byte chunks from the Google Drive API.
    For native Google Workspace files, exports as PDF.
    """
    creds = _build_google_credentials(refresh_token)

    # Force refresh to get a valid access token
    from google.auth.transport.requests import Request as GoogleAuthRequest
    creds.refresh(GoogleAuthRequest())

    headers = {"Authorization": f"Bearer {creds.token}"}

    is_native = is_google_apps_mime(mime_type)

    if is_native:
        export_mime = GOOGLE_APPS_EXPORT_MIME_MAP.get(mime_type, "application/pdf")
        url = GDRIVE_FILE_EXPORT_URL.format(file_id=gdrive_file_id, export_mime=export_mime)
    else:
        url = GDRIVE_FILE_DOWNLOAD_URL.format(file_id=gdrive_file_id)

    logger.info(f"Starting GDrive download: {url}")
    response = http_requests.get(url, headers=headers, stream=True, timeout=600)
    response.raise_for_status()

    for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
        if chunk:
            yield chunk


def _get_gdrive_file_metadata(gdrive_file_id: str, refresh_token: str) -> dict:
    """Fetch file metadata (name, mimeType, size) from Google Drive API."""
    creds = _build_google_credentials(refresh_token)

    from google.auth.transport.requests import Request as GoogleAuthRequest
    creds.refresh(GoogleAuthRequest())

    headers = {"Authorization": f"Bearer {creds.token}"}
    url = GDRIVE_FILE_META_URL.format(file_id=gdrive_file_id)
    params = {"fields": "id,name,mimeType,size"}
    response = http_requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


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

    Supports:
        - https://drive.google.com/file/d/FILE_ID/view
        - https://docs.google.com/document/d/FILE_ID/edit
        - https://docs.google.com/spreadsheets/d/FILE_ID/edit
        - https://docs.google.com/presentation/d/FILE_ID/edit
        - https://docs.google.com/drawings/d/FILE_ID/edit
        - https://drive.google.com/open?id=FILE_ID
        - https://drive.google.com/drive/folders/FOLDER_ID
        - Raw file ID string

    Args:
        url_or_id (str): The Google Drive URL or raw file ID string.

    Returns:
        str: The extracted Google Drive file ID.
    """
    clean = url_or_id.strip()

    # Match /d/FILE_ID pattern (covers /file/d/, /document/d/, /spreadsheets/d/, /presentation/d/, /drawings/d/)
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", clean)
    if match:
        return match.group(1)

    # Match /folders/FOLDER_ID pattern
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", clean)
    if match:
        return match.group(1)

    # Match ?id=FILE_ID or &id=FILE_ID (but NOT ouid=, etc.)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", clean)
    if match:
        return match.group(1)

    # If it's a URL but no pattern matched, take the last path segment
    if clean.startswith("http://") or clean.startswith("https://"):
        url_path = clean.split("?")[0]  # Strip query parameters
        parts = url_path.rstrip("/").split("/")
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
        raise IntegrationRequestError(message="Invalid Google Drive file ID or URL")

    # Verify dataset ownership
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.user_id == user.id, Dataset.is_deleted.is_(False)).one_or_none()
    if dataset is None:
        raise DatasetNotFoundError(message="Dataset not found or access denied")

    if folder_id:
        folder = db.query(Folder).filter(Folder.id == folder_id, Folder.user_id == user.id).one_or_none()
        if folder is None:
            folder_id = None

    # Verify user has a connected Google account with refresh token
    if not user.google_refresh_token:
        raise IntegrationConnectionError(message="Google account not connected. Please connect your Google account first via OAuth.")

    is_native_app = is_google_apps_mime(mime_type)

    # Fetch file metadata from Google Drive API to get real filename and mime type
    try:
        decrypted_refresh = decrypt_str(user.google_refresh_token)
        file_meta = _get_gdrive_file_metadata(gdrive_file_id, decrypted_refresh)
        logger.info(f"GDrive file metadata: {file_meta}")
        if not filename:
            filename = file_meta.get("name", f"gdrive_{gdrive_file_id}")
        if not mime_type:
            mime_type = file_meta.get("mimeType")
            is_native_app = is_google_apps_mime(mime_type)
        total_bytes = int(file_meta.get("size", 0)) if file_meta.get("size") else None
    except Exception as meta_err:
        logger.warning(f"Could not fetch GDrive metadata for {gdrive_file_id}: {meta_err}")
        total_bytes = None

    lock_key = f"lock:ingest:gdrive:{gdrive_file_id}"
    job_id = str(uuid4())

    # Redis SETNX Lock to prevent duplicate concurrent downloads of the same GDrive file
    lock_acquired = redis_server.set(lock_key, job_id, nx=True, ex=3600)
    if not lock_acquired:
        raise IntegrationRequestError(
            message="An ingestion job for this Google Drive file is already in progress.",
            http_status=409,
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

    # Pass the user's refresh token and mime_type so the background worker can download
    google_refresh_token = decrypt_str(user.google_refresh_token)

    background_tasks.add_task(
        process_gdrive_ingestion_job,
        job_id=job.id,
        lock_key=lock_key,
        stream_chunks_generator=stream_chunks_generator,
        filename=target_filename,
        total_bytes=total_bytes,
        user_id=user.id,
        dataset_id=dataset_id,
        folder_id=folder_id,
        is_native_google_app=is_native_app,
        mime_type=mime_type,
        gdrive_file_id=gdrive_file_id,
        google_refresh_token=google_refresh_token,
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
    gdrive_file_id: str | None = None,
    google_refresh_token: str | None = None,
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
        gdrive_file_id (str | None): Google Drive file ID for download.
        google_refresh_token (str | None): User's Google refresh token.
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

        # If no pre-built stream generator, create one from Google Drive API
        if stream_chunks_generator is None and gdrive_file_id and google_refresh_token:
            logger.info(f"Creating GDrive download stream for file_id={gdrive_file_id}")
            stream_chunks_generator = _stream_gdrive_download(
                gdrive_file_id=gdrive_file_id,
                refresh_token=google_refresh_token,
                mime_type=mime_type,
            )
        elif stream_chunks_generator is None:
            raise ValueError("No download stream available and no Google credentials provided")

        # 2. Prepare staging file and incremental SHA-256 calculation
        target_root = GDRIVE_STORAGE_DIR
        staging_file_id = str(uuid4())
        staging_path = target_root / f"{staging_file_id}.pending"
        target_root.mkdir(parents=True, exist_ok=True)

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
            final_path = _get_unique_filename(target_root, filename)
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
