"""Background worker service for Microsoft SharePoint / Graph API file ingestion."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import base64
import logging
import requests as http_requests
from fastapi import BackgroundTasks, HTTPException, status

from config.database import get_session_local
from config.redis_server import server as redis_server
from models.auth_model import User
from models.file_model import (
    AsyncIngestionJob,
    Dataset,
    DatasetFolderFilesMapping,
    Folder,
    ProviderHashMapping,
    UploadedFile,
    UploadedFileSourceType,
)
from schemas.file_schema import SharepointIngestResponse, SharepointItem, SharepointTreeResponse
from services.file_services import FINAL_ROOT, SHAREPOINT_STORAGE_DIR, _uploaded_filename, _get_unique_filename, _sync_dataset_status_from_mappings
from helpers.get_env import get_env

logger = logging.getLogger(__name__)


def encode_sharepoint_url(sharepoint_url: str) -> str:
    """
    Encode a public or organization SharePoint URL to Microsoft Graph sharing token format (`u!...`).

    Args:
        sharepoint_url (str): The raw SharePoint URL.

    Returns:
        str: Unpadded urlsafe base64 string prefixed with `u!`.
    """
    raw_bytes = sharepoint_url.strip().encode("utf-8")
    base64_str = base64.b64encode(raw_bytes).decode("utf-8")
    base64_url = base64_str.rstrip("=").replace("/", "_").replace("+", "-")
    return f"u!{base64_url}"


def get_user_microsoft_access_token(user: User) -> str:
    """Obtain a fresh Microsoft Graph API access token using the user's stored refresh token."""
    if not user.microsoft_refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Microsoft account not connected. Please authenticate via Microsoft OAuth.",
        )
    client_id = get_env("MICROSOFT_CLIENT_ID", default="", required=False).strip()
    client_secret = get_env("MICROSOFT_CLIENT_SECRET", default="", required=False).strip()
    tenant = get_env("MICROSOFT_TENANT_ID", default="common", required=False).strip() or "common"

    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": user.microsoft_refresh_token,
        "scope": "offline_access User.Read Files.Read.All Sites.Read.All",
    }
    resp = http_requests.post(token_url, data=payload, timeout=30)
    if resp.status_code != 200:
        logger.error(f"Failed to refresh Microsoft token: {resp.text}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to refresh Microsoft access token. Please re-connect your Microsoft account.",
        )
    return resp.json().get("access_token")


def _get_sharepoint_item_metadata(file_id_or_url: str, access_token: str) -> dict:
    """Fetch item metadata (name, size, hashes, downloadUrl) from Microsoft Graph API."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if file_id_or_url.startswith("http://") or file_id_or_url.startswith("https://"):
        sharing_token = encode_sharepoint_url(file_id_or_url)
        url = f"https://graph.microsoft.com/v1.0/shares/{sharing_token}/driveItem"
    else:
        url = f"https://graph.microsoft.com/v1.0/me/drive/items/{file_id_or_url}"

    resp = http_requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _stream_sharepoint_download(file_id_or_url: str, access_token: str, download_url: str | None = None):
    """Stream download file chunks from Microsoft Graph API without sending Authorization headers to location redirects."""
    if download_url:
        logger.info(f"Starting SharePoint download using direct pre-authenticated URL...")
        resp = http_requests.get(download_url, stream=True, timeout=600)
        resp.raise_for_status()
    else:
        headers = {"Authorization": f"Bearer {access_token}"}
        if file_id_or_url.startswith("http://") or file_id_or_url.startswith("https://"):
            sharing_token = encode_sharepoint_url(file_id_or_url)
            target_url = f"https://graph.microsoft.com/v1.0/shares/{sharing_token}/driveItem/content"
        else:
            target_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{file_id_or_url}/content"

        logger.info(f"Starting SharePoint download stream from: {target_url[:80]}...")
        resp = http_requests.get(target_url, headers=headers, allow_redirects=False, timeout=30)
        if resp.status_code in (301, 302, 303, 307, 308) and "Location" in resp.headers:
            redirect_url = resp.headers["Location"]
            logger.info("Following Graph API download redirect without Authorization header...")
            resp = http_requests.get(redirect_url, stream=True, timeout=600)
            resp.raise_for_status()
        else:
            resp.raise_for_status()

    for chunk in resp.iter_content(chunk_size=8192):
        if chunk:
            yield chunk


def initiate_sharepoint_ingestion(
    db: Session,
    user: User,
    dataset_id: str,
    file_id_or_url: str,
    background_tasks: BackgroundTasks,
    quick_xor_hash: str | None = None,
    folder_id: str | None = None,
    filename: str | None = None,
    stream_chunks_generator = None,
) -> SharepointIngestResponse:
    """
    Initiate SharePoint file ingestion with zero-I/O Rosetta Stone pre-flight lookup and Redis SETNX locks.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        dataset_id (str): Target dataset ID.
        file_id_or_url (str): SharePoint item ID or shared URL.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        quick_xor_hash (str | None): Optional Microsoft quickXorHash string for Rosetta Stone lookup.
        folder_id (str | None): Optional virtual folder ID.
        filename (str | None): Optional override filename.
        stream_chunks_generator (iterable | None): Optional pre-configured byte stream generator.

    Returns:
        SharepointIngestResponse: Details of the queued or instantly completed ingestion job.

    Raises:
        HTTPException: If dataset is missing or a Redis lock exists.
    """
    # Verify dataset ownership
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.user_id == user.id, Dataset.is_deleted.is_(False)).one_or_none()
    if dataset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found or access denied")

    if folder_id:
        folder = db.query(Folder).filter(Folder.id == folder_id, Folder.user_id == user.id).one_or_none()
        if folder is None:
            folder_id = None

    # Verify user has connected Microsoft account
    if not user.microsoft_refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Microsoft account not connected. Please authenticate via Microsoft OAuth.",
        )

    # Fetch Microsoft access token
    access_token = get_user_microsoft_access_token(user)

    # Fetch real item metadata from Microsoft Graph API
    download_url = None
    total_bytes = None
    try:
        meta = _get_sharepoint_item_metadata(file_id_or_url, access_token)
        logger.info(f"SharePoint item metadata: name={meta.get('name')}, size={meta.get('size')}")
        if not filename:
            filename = meta.get("name")
        total_bytes = int(meta.get("size", 0)) if meta.get("size") else None
        if not quick_xor_hash:
            quick_xor_hash = meta.get("file", {}).get("hashes", {}).get("quickXorHash")
        download_url = meta.get("@microsoft.graph.downloadUrl")
    except Exception as meta_err:
        logger.warning(f"Could not fetch SharePoint metadata for {file_id_or_url}: {meta_err}")

    target_filename = filename or f"sharepoint_{uuid4().hex[:8]}"

    # Determine provider_file_id / sharing token
    if file_id_or_url.startswith("http://") or file_id_or_url.startswith("https://"):
        provider_file_id = encode_sharepoint_url(file_id_or_url)
    else:
        provider_file_id = file_id_or_url.strip()

    # 1. Zero-I/O Rosetta Stone Pre-Flight Lookup
    if quick_xor_hash and quick_xor_hash.strip():
        clean_hash = quick_xor_hash.strip()
        mapping = (
            db.query(ProviderHashMapping)
            .filter(
                ProviderHashMapping.provider_name == "Sharepoint",
                ProviderHashMapping.provider_hash == clean_hash,
            )
            .one_or_none()
        )
        if mapping is not None:
            uploaded_file = (
                db.query(UploadedFile)
                .filter(UploadedFile.master_hash == mapping.master_hash)
                .first()
            )
            if uploaded_file is not None and Path(uploaded_file.physical_path).exists():
                # Zero-I/O Hit! Instantly map file without network or disk I/O
                job_id = str(uuid4())
                job = AsyncIngestionJob(
                    id=job_id,
                    user_id=user.id,
                    dataset_id=dataset_id,
                    folder_id=folder_id,
                    provider="Sharepoint",
                    source_url_or_id=provider_file_id,
                    filename=target_filename,
                    status="completed",
                    progress_percentage=100,
                    master_hash=uploaded_file.master_hash,
                    file_id=uploaded_file.id,
                )
                db.add(job)

                stmt = (
                    pg_insert(DatasetFolderFilesMapping)
                    .values(
                        dataset_id=dataset_id,
                        folder_id=folder_id,
                        file_id=uploaded_file.id,
                        user_id=user.id,
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_dataset_folder_file",
                    )
                )
                db.execute(stmt)

                _sync_dataset_status_from_mappings(db, dataset)
                db.commit()

                return SharepointIngestResponse(
                    job_id=job.id,
                    status="completed",
                    message="File instantly ingested via Rosetta Stone zero-I/O hash lookup",
                    is_instant_deduplicated=True,
                )

    # 2. Rosetta Stone Miss: Acquire Redis SETNX Lock and Queue Background Task
    lock_key = f"lock:ingest:sharepoint:{provider_file_id}"
    job_id = str(uuid4())

    lock_acquired = redis_server.set(lock_key, job_id, nx=True, ex=3600)
    if not lock_acquired:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An ingestion job for this SharePoint file is already in progress.",
        )

    job = AsyncIngestionJob(
        id=job_id,
        user_id=user.id,
        dataset_id=dataset_id,
        folder_id=folder_id,
        provider="Sharepoint",
        source_url_or_id=provider_file_id,
        filename=target_filename,
        status="pending",
        progress_percentage=0,
    )
    db.add(job)
    db.commit()

    microsoft_refresh_token = user.microsoft_refresh_token

    background_tasks.add_task(
        process_sharepoint_ingestion_job,
        job_id=job.id,
        lock_key=lock_key,
        stream_chunks_generator=stream_chunks_generator,
        filename=target_filename,
        total_bytes=total_bytes,
        user_id=user.id,
        dataset_id=dataset_id,
        provider_file_id=provider_file_id,
        quick_xor_hash=quick_xor_hash,
        folder_id=folder_id,
        file_id_or_url=file_id_or_url,
        microsoft_refresh_token=microsoft_refresh_token,
        download_url=download_url,
    )

    return SharepointIngestResponse(
        job_id=job.id,
        status="pending",
        message="SharePoint ingestion job initiated",
        is_instant_deduplicated=False,
    )


def get_sharepoint_tree(
    db: Session,
    user: User,
    folder_id: str | None = None,
    drive_id: str | None = None,
    site_id: str | None = None,
    raw_graph_items: list[dict] | None = None,
) -> SharepointTreeResponse:
    """
    Query or structure Microsoft SharePoint / Graph API directory tree items.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        folder_id (str | None): Target folder item ID.
        drive_id (str | None): Optional target drive ID.
        site_id (str | None): Optional target site ID.
        raw_graph_items (list[dict] | None): Optional direct payload items for testing/mocking.

    Returns:
        SharepointTreeResponse: Parsed directory items.

    Raises:
        HTTPException: If the user has not connected their Microsoft account.
    """
    if not user.microsoft_refresh_token and raw_graph_items is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Microsoft account not connected. Please authenticate via Microsoft OAuth.",
        )

    if raw_graph_items is None and user.microsoft_refresh_token:
        try:
            access_token = get_user_microsoft_access_token(user)
            headers = {"Authorization": f"Bearer {access_token}"}
            if folder_id:
                graph_url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}/children"
            elif drive_id:
                graph_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root/children"
            else:
                graph_url = "https://graph.microsoft.com/v1.0/me/drive/root/children"

            resp = http_requests.get(graph_url, headers=headers, timeout=30)
            if resp.status_code == 200:
                raw_graph_items = resp.json().get("value", [])
            else:
                logger.warning(f"Graph API children call returned {resp.status_code}: {resp.text}")
                raw_graph_items = []
        except Exception as err:
            logger.warning(f"Failed to fetch Graph API items: {err}")
            raw_graph_items = []

    items: list[SharepointItem] = []
    source_items = raw_graph_items or []

    for raw in source_items:
        is_folder = "folder" in raw or raw.get("is_folder", False)
        hashes = raw.get("file", {}).get("hashes", {})
        quick_hash = hashes.get("quickXorHash") or raw.get("quick_xor_hash")

        item = SharepointItem(
            id=str(raw.get("id")),
            name=str(raw.get("name", "Untitled")),
            is_folder=bool(is_folder),
            mime_type=raw.get("file", {}).get("mimeType") or raw.get("mime_type"),
            size_bytes=int(raw.get("size", raw.get("size_bytes", 0))),
            quick_xor_hash=quick_hash,
            web_url=raw.get("webUrl") or raw.get("web_url"),
            parent_id=folder_id,
        )
        items.append(item)

    return SharepointTreeResponse(
        items=items,
        drive_id=drive_id,
        parent_folder_id=folder_id,
    )


def process_sharepoint_ingestion_job(
    job_id: str,
    lock_key: str,
    stream_chunks_generator,
    filename: str,
    total_bytes: int | None,
    user_id: str,
    dataset_id: str,
    provider_file_id: str,
    quick_xor_hash: str | None = None,
    folder_id: str | None = None,
    file_id_or_url: str | None = None,
    microsoft_refresh_token: str | None = None,
    download_url: str | None = None,
) -> None:
    """
    Background worker to stream SharePoint file content, record Rosetta Stone hash mapping, and commit atomically.

    Args:
        job_id (str): Unique AsyncIngestionJob identifier.
        lock_key (str): Redis distributed lock key to release upon completion.
        stream_chunks_generator (iterable): Generator yielding raw byte chunks.
        filename (str): Target filename for storage.
        total_bytes (int | None): Expected total byte size if known.
        user_id (str): Owner user ID.
        dataset_id (str): Target dataset ID.
        provider_file_id (str): SharePoint drive item ID.
        quick_xor_hash (str | None): Optional Microsoft quickXorHash string.
        folder_id (str | None): Optional target folder ID.
        file_id_or_url (str | None): Original file ID or URL string.
        microsoft_refresh_token (str | None): User's Microsoft OAuth refresh token.
        download_url (str | None): Pre-authenticated direct download URL if known.
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

        # Create download stream from Microsoft Graph API if no pre-built generator is provided
        if stream_chunks_generator is None and file_id_or_url and microsoft_refresh_token:
            logger.info(f"Creating SharePoint download stream for target={file_id_or_url[:60]}")
            client_id = get_env("MICROSOFT_CLIENT_ID", default="", required=False).strip()
            client_secret = get_env("MICROSOFT_CLIENT_SECRET", default="", required=False).strip()
            tenant = get_env("MICROSOFT_TENANT_ID", default="common", required=False).strip() or "common"
            token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
            payload = {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": microsoft_refresh_token,
                "scope": "offline_access User.Read Files.Read.All Sites.Read.All",
            }
            resp = http_requests.post(token_url, data=payload, timeout=30)
            if resp.status_code == 200:
                access_token = resp.json().get("access_token")
                stream_chunks_generator = _stream_sharepoint_download(
                    file_id_or_url=file_id_or_url,
                    access_token=access_token,
                    download_url=download_url,
                )
            else:
                raise ValueError(f"Failed to refresh Microsoft token: {resp.text}")
        elif stream_chunks_generator is None:
            raise ValueError("No download stream available and no Microsoft credentials provided")

        # 2. Prepare staging file and incremental SHA-256 calculation
        target_root = SHAREPOINT_STORAGE_DIR
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

            # Force hardware disk sync before renaming
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
            # Re-use existing physical file and remove staging file
            final_file_id = uploaded_file.id
            if staging_path.exists():
                staging_path.unlink()
        else:
            final_file_id = str(uuid4())
            final_path = _get_unique_filename(target_root, filename)
            staging_path.replace(final_path)

            new_uploaded_file = UploadedFile(
                id=final_file_id,
                filename=filename,
                file_size_bytes=downloaded_bytes,
                master_hash=master_hash,
                physical_path=str(final_path),
                source_type=UploadedFileSourceType.Sharepoint,
            )
            db.add(new_uploaded_file)
            db.flush()

        # 4. Rosetta Stone Learning Step: Map quickXorHash -> master_hash if provided
        if quick_xor_hash and quick_xor_hash.strip():
            clean_hash = quick_xor_hash.strip()
            existing_mapping = (
                db.query(ProviderHashMapping)
                .filter(
                    ProviderHashMapping.provider_name == "Sharepoint",
                    ProviderHashMapping.provider_hash == clean_hash,
                )
                .one_or_none()
            )
            if existing_mapping is None:
                new_hash_mapping = ProviderHashMapping(
                    provider_name="Sharepoint",
                    provider_file_id=provider_file_id,
                    provider_hash=clean_hash,
                    master_hash=master_hash,
                )
                db.add(new_hash_mapping)
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
