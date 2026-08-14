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
from fastapi import BackgroundTasks

from config.database import get_session_local
from config.redis_server import server as redis_server
from models.auth_model import User
from models.file_model import (
    Dataset,
    IngestedFile,
    IngestedFileProviderType,
)
from schemas.file_schema import SharepointIngestResponse, SharepointItem, SharepointTreeResponse
from services.file_services import FINAL_ROOT, SHAREPOINT_STORAGE_DIR, _uploaded_filename, _get_unique_filename, _sync_dataset_status_from_mappings
from helpers.get_env import get_env
from helpers.crypto import decrypt_str
from utils.errors import DatasetNotFoundError, IntegrationConnectionError, IntegrationRequestError

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
        raise IntegrationConnectionError(message="Microsoft account not connected. Please authenticate via Microsoft OAuth.")
    client_id = get_env("MICROSOFT_CLIENT_ID", default="", required=False).strip()
    client_secret = get_env("MICROSOFT_CLIENT_SECRET", default="", required=False).strip()
    tenant = get_env("MICROSOFT_TENANT_ID", default="common", required=False).strip() or "common"

    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    # decrypt stored refresh token before sending to Microsoft
    refresh_token = decrypt_str(user.microsoft_refresh_token)
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "offline_access User.Read Files.Read.All Sites.Read.All",
    }
    resp = http_requests.post(token_url, data=payload, timeout=30)
    if resp.status_code != 200:
        logger.error(f"Failed to refresh Microsoft token: {resp.text}")
        raise IntegrationRequestError(message="Failed to refresh Microsoft access token. Please re-connect your Microsoft account.")
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
    """Initiate an asynchronous SharePoint ingestion job."""
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.user_id == user.id, Dataset.is_deleted.is_(False)).one_or_none()
    if dataset is None:
        raise DatasetNotFoundError(message="Dataset not found or access denied")

    if not user.microsoft_refresh_token:
        raise IntegrationConnectionError(message="Microsoft account not connected. Please authenticate via Microsoft OAuth.")

    access_token = get_user_microsoft_access_token(user)

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

    if file_id_or_url.startswith("http://") or file_id_or_url.startswith("https://"):
        provider_file_id = encode_sharepoint_url(file_id_or_url)
    else:
        provider_file_id = file_id_or_url.strip()

    # 1. Rosetta Stone Pre-Flight Lookup
    if quick_xor_hash and quick_xor_hash.strip():
        clean_hash = quick_xor_hash.strip()
        existing = (
            db.query(IngestedFile)
            .filter(
                IngestedFile.provider == IngestedFileProviderType.Sharepoint,
                IngestedFile.provider_hash == clean_hash,
                IngestedFile.status == "completed",
            )
            .first()
        )
        if existing and existing.physical_path:
            job_id = str(uuid4())
            job = IngestedFile(
                id=job_id,
                user_id=user.id,
                dataset_id=dataset_id,
                provider=IngestedFileProviderType.Sharepoint,
                file_path=provider_file_id,
                filename=target_filename,
                physical_path=existing.physical_path,
                file_size_bytes=existing.file_size_bytes,
                master_hash=existing.master_hash,
                provider_hash=clean_hash,
                status="completed",
            )
            db.add(job)
            _sync_dataset_status_from_mappings(db, dataset)
            db.commit()

            return SharepointIngestResponse(
                job_id=job.id,
                status="completed",
                message="File instantly ingested via Rosetta Stone zero-I/O hash lookup",
                is_instant_deduplicated=True,
            )

    lock_key = f"lock:ingest:sharepoint:{provider_file_id}"
    job_id = str(uuid4())

    lock_acquired = redis_server.set(lock_key, job_id, nx=True, ex=3600)
    if not lock_acquired:
        raise IntegrationRequestError(
            message="An ingestion job for this SharePoint file is already in progress.",
            http_status=409,
        )

    job = IngestedFile(
        id=job_id,
        user_id=user.id,
        dataset_id=dataset_id,
        provider=IngestedFileProviderType.Sharepoint,
        file_path=provider_file_id,
        filename=target_filename,
        provider_hash=quick_xor_hash.strip() if quick_xor_hash else None,
        status="pending",
        file_size_bytes=total_bytes,
    )
    db.add(job)
    db.commit()

    background_tasks.add_task(
        process_sharepoint_ingestion_job,
        job_id=job.id,
        lock_key=lock_key,
        stream_chunks_generator=stream_chunks_generator,
        filename=target_filename,
        total_bytes=total_bytes,
        user_id=user.id,
        dataset_id=dataset_id,
        folder_id=folder_id,
        quick_xor_hash=quick_xor_hash,
        provider_file_id=provider_file_id,
        download_url=download_url,
    )

    return SharepointIngestResponse(
        job_id=job.id,
        status="pending",
        message="SharePoint ingestion job initiated",
        is_instant_deduplicated=False,
    )


def process_sharepoint_ingestion_job(
    job_id: str,
    lock_key: str,
    stream_chunks_generator,
    filename: str,
    total_bytes: int | None,
    user_id: str,
    dataset_id: str,
    folder_id: str | None = None,
    quick_xor_hash: str | None = None,
    provider_file_id: str | None = None,
    download_url: str | None = None,
) -> None:
    """Background worker to stream SharePoint content and update IngestedFile record."""
    db: Session = get_session_local()()
    staging_path: Path | None = None
    progress_key = f"ingest:{job_id}:progress"

    try:
        job = db.query(IngestedFile).filter(IngestedFile.id == job_id).one_or_none()
        if job:
            job.status = "in_progress"
            db.commit()

        redis_server.set(progress_key, "0")

        if stream_chunks_generator is None and (download_url or provider_file_id):
            user = db.query(User).filter(User.id == user_id).first()
            if not user or not user.microsoft_refresh_token:
                raise ValueError("User Microsoft account disconnected")
            access_token = get_user_microsoft_access_token(user)

            if not download_url and provider_file_id:
                meta = _get_sharepoint_item_metadata(provider_file_id, access_token)
                download_url = meta.get("@microsoft.graph.downloadUrl")

            if download_url:
                resp = http_requests.get(download_url, stream=True)
                if resp.status_code != 200:
                    raise ValueError(f"SharePoint download HTTP {resp.status_code}: {resp.text}")
                stream_chunks_generator = resp.iter_content(chunk_size=8192)

        if stream_chunks_generator is None:
            raise ValueError("No download stream available and no Microsoft credentials provided")

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

            destination.flush()
            os.fsync(destination.fileno())

        master_hash = sha256_hash.hexdigest()

        existing_file = (
            db.query(IngestedFile)
            .filter(IngestedFile.master_hash == master_hash, IngestedFile.status == "completed")
            .first()
        )

        final_path_str: str
        if existing_file and existing_file.physical_path:
            final_path_str = existing_file.physical_path
            if staging_path.exists():
                staging_path.unlink()
        else:
            final_path = _get_unique_filename(target_root, filename)
            staging_path.replace(final_path)
            final_path_str = str(final_path)

        job = db.query(IngestedFile).filter(IngestedFile.id == job_id).one_or_none()
        if job:
            job.status = "completed"
            job.file_size_bytes = downloaded_bytes
            job.physical_path = final_path_str
            job.master_hash = master_hash
            if quick_xor_hash and quick_xor_hash.strip():
                job.provider_hash = quick_xor_hash.strip()
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

        job = db.query(IngestedFile).filter(IngestedFile.id == job_id).one_or_none()
        if job:
            job.status = "failed"
            job.error_message = str(e)
            db.commit()

    finally:
        db.close()
        redis_server.delete(lock_key)
        redis_server.delete(progress_key)
