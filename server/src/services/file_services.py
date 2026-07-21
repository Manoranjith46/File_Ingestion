"""Business logic for chunked file uploads and deduplication."""

from __future__ import annotations

import hashlib
import shutil
from math import ceil
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.redis_server import server as redis_server
from helpers.get_env import get_env
from models.auth_model import User
from models.file_model import PhysicalFile, UserUploadMapping
from schemas.file_schema import (
    CHUNK_SIZE_BYTES,
    UploadChunkRequest,
    UploadChunkResponse,
    UploadFinalizeResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadListItem,
    UploadListResponse,
)

UPLOAD_ROOT = Path(get_env("UPLOAD_STORAGE_DIR", default=str(Path(__file__).resolve().parents[2] / "uploads"), required=False))
PARTS_ROOT = UPLOAD_ROOT / ".parts"
FINAL_ROOT = UPLOAD_ROOT
SESSION_TTL_SECONDS = int(get_env("UPLOAD_SESSION_TTL_SECONDS", default="3600", required=False))
LOCK_TTL_SECONDS = 5


def _ensure_storage_dirs() -> None:
    """Create the upload directories if they do not exist yet."""
    PARTS_ROOT.mkdir(parents=True, exist_ok=True)
    FINAL_ROOT.mkdir(parents=True, exist_ok=True)


def _meta_key(upload_id: str) -> str:
    return f"upload:{upload_id}:meta"


def _bitmap_key(upload_id: str) -> str:
    return f"upload:{upload_id}:bitmap"


def _hashes_key(upload_id: str) -> str:
    return f"upload:{upload_id}:chunk_hashes"


def _lock_key(upload_id: str) -> str:
    return f"upload:{upload_id}:lock"


def _parts_dir(upload_id: str) -> Path:
    return PARTS_ROOT / upload_id


def _final_file_path(filename: str) -> Path:
    """Build the on-disk path for a merged upload using the client filename."""
    safe_name = Path(filename).name.strip()
    if not safe_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filename is required")
    return FINAL_ROOT / safe_name


def _chunk_path(upload_id: str, chunk_index: int) -> Path:
    return _parts_dir(upload_id) / f"{chunk_index:06d}.part"


def _acquire_lock(upload_id: str, owner: str) -> None:
    """Acquire a short-lived Redis lock for a single upload.

    Raises:
        HTTPException: If another worker already owns the lock.
    """
    if not redis_server.set(_lock_key(upload_id), owner, nx=True, ex=LOCK_TTL_SECONDS):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload is busy")


def _release_lock(upload_id: str, owner: str) -> None:
    """Release the Redis lock when the current owner still holds it."""
    lock_key = _lock_key(upload_id)
    if redis_server.get(lock_key) == owner:
        redis_server.delete(lock_key)


def _load_upload_state(upload_id: str) -> dict[str, str]:
    """Load the upload metadata from Redis.

    Raises:
        HTTPException: If the upload session no longer exists.
    """
    meta = redis_server.hgetall(_meta_key(upload_id))
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid upload ID")
    return meta


def _parse_int(value: str | None, default: int = 0) -> int:
    """Convert a string value to int with a fallback."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def initialize_upload(db: Session, user: User, payload: UploadInitRequest) -> UploadInitResponse:
    """Initialize a chunked upload session.

    Args:
        db: The active database session.
        user: The authenticated user starting the upload.
        payload: The upload initialization payload.

    Returns:
        UploadInitResponse: The upload session metadata.
    """
    _ensure_storage_dirs()
    upload_id = str(uuid4())
    total_chunks = ceil(payload.filesize / CHUNK_SIZE_BYTES)
    parts_dir = _parts_dir(upload_id)
    parts_dir.mkdir(parents=True, exist_ok=True)

    redis_server.hset(
        _meta_key(upload_id),
        mapping={
            "user_id": user.id,
            "filename": payload.filename,
            "filesize": str(payload.filesize),
            "total_chunks": str(total_chunks),
            "chunk_size": str(CHUNK_SIZE_BYTES),
            "status": "initialized",
        },
    )
    redis_server.expire(_meta_key(upload_id), SESSION_TTL_SECONDS)
    redis_server.expire(_bitmap_key(upload_id), SESSION_TTL_SECONDS)
    redis_server.expire(_hashes_key(upload_id), SESSION_TTL_SECONDS)
    return UploadInitResponse(
        upload_id=upload_id,
        filename=payload.filename,
        filesize=payload.filesize,
        chunk_size=CHUNK_SIZE_BYTES,
        total_chunks=total_chunks,
    )


def process_upload_chunk(db: Session, user: User, payload: UploadChunkRequest, chunk_bytes: bytes) -> UploadChunkResponse:
    """Validate and persist a single upload chunk.

    Args:
        db: The active database session.
        user: The authenticated user uploading the chunk.
        payload: The chunk metadata.
        chunk_bytes: The uploaded chunk contents.

    Returns:
        UploadChunkResponse: The chunk processing state.
    """
    state = _load_upload_state(payload.upload_id)
    if state.get("user_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload does not belong to the current user")

    total_chunks = _parse_int(state.get("total_chunks"))
    if payload.chunk_index < 0 or payload.chunk_index >= total_chunks:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chunk index outside the valid range")

    bitmap_key = _bitmap_key(payload.upload_id)
    hashes_key = _hashes_key(payload.upload_id)
    received_chunks = int(redis_server.bitcount(bitmap_key) or 0)
    stored_hash = redis_server.hget(hashes_key, str(payload.chunk_index))
    is_duplicate = bool(redis_server.getbit(bitmap_key, payload.chunk_index))

    chunk_hash = hashlib.sha256(chunk_bytes).hexdigest()
    if chunk_hash != payload.chunk_hash:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chunk hash mismatch")

    if is_duplicate:
        if stored_hash != payload.chunk_hash:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chunk hash mismatch")
        return UploadChunkResponse(
            status="success",
            upload_id=payload.upload_id,
            chunk_index=payload.chunk_index,
            bytes_received=len(chunk_bytes),
            received_chunks=received_chunks,
            total_chunks=total_chunks,
            complete=received_chunks == total_chunks,
        )

    if payload.chunk_index != received_chunks:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Missing prior chunks during sequential write")

    owner = user.id
    _acquire_lock(payload.upload_id, owner)
    try:
        chunk_path = _chunk_path(payload.upload_id, payload.chunk_index)
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_path.write_bytes(chunk_bytes)
        redis_server.hset(hashes_key, str(payload.chunk_index), payload.chunk_hash)
        redis_server.setbit(bitmap_key, payload.chunk_index, 1)
        redis_server.expire(bitmap_key, SESSION_TTL_SECONDS)
        redis_server.expire(hashes_key, SESSION_TTL_SECONDS)
        redis_server.expire(_meta_key(payload.upload_id), SESSION_TTL_SECONDS)

        received_chunks = int(redis_server.bitcount(bitmap_key) or 0)
        return UploadChunkResponse(
            status="success",
            upload_id=payload.upload_id,
            chunk_index=payload.chunk_index,
            bytes_received=len(chunk_bytes),
            received_chunks=received_chunks,
            total_chunks=total_chunks,
            complete=received_chunks == total_chunks,
        )
    finally:
        _release_lock(payload.upload_id, owner)


def _compute_file_hashes(file_path: Path) -> tuple[str, str]:
    """Compute the master hash and fingerprint for a merged file."""
    hasher = hashlib.sha256()
    first_block = b""
    last_block = b""

    with file_path.open("rb") as file_handle:
        while True:
            block = file_handle.read(CHUNK_SIZE_BYTES)
            if not block:
                break
            if not first_block:
                first_block = block
            last_block = block
            hasher.update(block)

    master_hash = hasher.hexdigest()
    file_fingerprint = hashlib.sha256(first_block + last_block).hexdigest()
    return master_hash, file_fingerprint


def finalize_upload(db: Session, user: User, upload_id: str) -> UploadFinalizeResponse:
    """Merge completed chunks into a deduplicated physical file.

    Args:
        db: The active database session.
        user: The authenticated user finalizing the upload.
        upload_id: The upload session identifier.

    Returns:
        UploadFinalizeResponse: The persisted physical file metadata.
    """
    state = _load_upload_state(upload_id)
    if state.get("user_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload does not belong to the current user")

    total_chunks = _parse_int(state.get("total_chunks"))
    bitmap_key = _bitmap_key(upload_id)
    received_chunks = int(redis_server.bitcount(bitmap_key) or 0)
    if received_chunks != total_chunks:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Missing prior chunks during final merge")

    owner = user.id
    _acquire_lock(upload_id, owner)
    parts_dir = _parts_dir(upload_id)
    staging_path = FINAL_ROOT / f"{upload_id}.staging"
    final_filename = state.get("filename", upload_id)
    final_path = _final_file_path(final_filename)
    try:
        with staging_path.open("wb") as destination:
            for chunk_index in range(total_chunks):
                chunk_path = _chunk_path(upload_id, chunk_index)
                if not chunk_path.exists():
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Missing prior chunks during final merge")
                with chunk_path.open("rb") as source:
                    shutil.copyfileobj(source, destination)

        master_hash, file_fingerprint = _compute_file_hashes(staging_path)
        file_size_bytes = staging_path.stat().st_size
        existing_file = db.query(PhysicalFile).filter(PhysicalFile.master_hash == master_hash).one_or_none()

        if existing_file is None:
            if final_path.exists():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A file with the same name already exists")
            staging_path.replace(final_path)
            physical_file = PhysicalFile(
                master_hash=master_hash,
                file_fingerprint=file_fingerprint,
                file_size_bytes=file_size_bytes,
                file_path=str(final_path),
            )
            db.add(physical_file)
            db.flush()
        else:
            physical_file = existing_file
            staging_path.unlink(missing_ok=True)

        mapping = UserUploadMapping(
            user_id=user.id,
            physical_file_id=physical_file.id,
            client_filename=state.get("filename", upload_id),
        )
        db.add(mapping)
        db.commit()

        shutil.rmtree(parts_dir, ignore_errors=True)
        redis_server.delete(_meta_key(upload_id), bitmap_key, _hashes_key(upload_id), _lock_key(upload_id))

        return UploadFinalizeResponse(
            status="success",
            upload_id=upload_id,
            physical_file_id=physical_file.id,
            file_path=physical_file.file_path,
            master_hash=physical_file.master_hash,
            complete=True,
        )
    finally:
        _release_lock(upload_id, owner)


def list_user_uploads(db: Session, user: User, page: int = 1, limit: int = 10) -> UploadListResponse:
    """Return paginated uploads for the authenticated user.

    Args:
        db: The active database session.
        user: The authenticated user.
        page: The page number to return.
        limit: The maximum number of rows per page.

    Returns:
        UploadListResponse: The paginated upload metadata.
    """
    safe_page = max(page, 1)
    safe_limit = max(limit, 1)
    query = (
        db.query(UserUploadMapping, PhysicalFile)
        .join(PhysicalFile, UserUploadMapping.physical_file_id == PhysicalFile.id)
        .filter(UserUploadMapping.user_id == user.id)
        .order_by(UserUploadMapping.uploaded_at.desc())
    )

    total_files = query.count()
    rows = query.offset((safe_page - 1) * safe_limit).limit(safe_limit).all()
    files = [
        UploadListItem(filename=mapping.client_filename, filesize=physical_file.file_size_bytes)
        for mapping, physical_file in rows
    ]
    return UploadListResponse(page=safe_page, limit=safe_limit, total_files=total_files, files=files)