"""Business logic for chunked file upload workflows."""


from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from math import ceil
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config.redis_server import server as redis_server, atomic_chunk_state
from helpers.get_env import get_env
from models.auth_model import User
from models.file_model import Folder, UploadedFile, Dataset, DatasetFolderFilesMapping
from schemas.file_schema import (
    CHUNK_SIZE_BYTES,
    UploadDeleteResponse,
    UploadFinalizeRequest,
    UploadFinalizeResponse,
    UploadChunkRequest,
    UploadChunkResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadsTreeResponse,
    DatasetCreate,
    DatasetUpdate,
    DatasetAttachFileRequest,
    DatasetAttachFileResponse,
)

UPLOAD_ROOT = Path(get_env("UPLOAD_STORAGE_DIR", default=str(Path(__file__).resolve().parents[2] / "uploads"), required=False))
PARTS_ROOT = UPLOAD_ROOT / ".parts"
FINAL_ROOT = UPLOAD_ROOT / "files"
SESSION_TTL_SECONDS = int(get_env("UPLOAD_SESSION_TTL_SECONDS", default="3600", required=False))


def _ensure_storage_dirs() -> None:
    """Ensure upload storage directories exist."""
    PARTS_ROOT.mkdir(parents=True, exist_ok=True)
    FINAL_ROOT.mkdir(parents=True, exist_ok=True)


def _meta_key(upload_id: str) -> str:
    return f"upload:{upload_id}:meta"


def _bitmap_key(upload_id: str) -> str:
    return f"upload:{upload_id}:bitmap"


def _chunk_hashes_key(upload_id: str) -> str:
    return f"upload:{upload_id}:chunk_hashes"



def _parts_dir(upload_id: str) -> Path:
    return PARTS_ROOT / upload_id


def _chunk_path(upload_id: str, chunk_index: int) -> Path:
    return _parts_dir(upload_id) / f"{chunk_index:06d}.part"


def _file_path(file_id: str) -> Path:
    return FINAL_ROOT / f"{file_id}"


def _uploaded_filename(filename: str) -> str:
    """Return the storage filename derived from the client-supplied name."""
    return Path(filename).name


def _validate_relative_path(relative_path: str | None) -> list[str]:
    if relative_path is None or relative_path.strip() == "":
        return []

    normalized = relative_path.replace("\\", "/").strip()
    if normalized.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="relative_path must be relative")

    parts = [segment.strip() for segment in normalized.split("/") if segment.strip() and segment != "."]
    if any(part == ".." for part in parts):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="relative_path must not contain traversal segments")

    return parts


def _resolve_folder_parts(relative_path: str | None, filename: str | None = None) -> list[str]:
    """
    Resolve the folder segments from an upload path.

    Args:
        relative_path (str | None): The client-supplied relative path.
        filename (str | None): The original file name, used to strip a trailing file segment.

    Returns:
        list[str]: The folder path segments.
    """
    parts = _validate_relative_path(relative_path)
    if not parts:
        return []

    if filename is not None and parts[-1] == Path(filename).name:
        return parts[:-1]

    return parts


def _get_folder_tree(db: Session, user: User, relative_path: str | None, filename: str | None = None) -> Folder | None:
    parts = _resolve_folder_parts(relative_path, filename)
    if not parts:
        return None

    parent_id: str | None = None
    folder: Folder | None = None
    for part in parts:
        folder = (
            db.query(Folder)
            .filter(Folder.user_id == user.id, Folder.parent_id == parent_id, Folder.name == part)
            .one_or_none()
        )
        if folder is None:
            folder = Folder(user_id=user.id, name=part, parent_id=parent_id)
            db.add(folder)
            db.flush()
        parent_id = folder.id
    return folder


def _get_folder_by_id(db: Session, user: User, folder_id: str) -> Folder:
    """
    Resolve a folder by ID for the current user.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        folder_id (str): The folder ID to resolve.

    Returns:
        Folder: The matching folder row.

    Raises:
        HTTPException: If the folder does not exist or does not belong to the user.
    """
    folder = db.query(Folder).filter(Folder.id == folder_id, Folder.user_id == user.id).one_or_none()
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
    return folder


def _collect_descendant_folder_ids(db: Session, user: User, folder_id: str) -> set[str]:
    """
    Collect a folder and all of its descendant folder IDs.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        folder_id (str): The root folder ID.

    Returns:
        set[str]: The folder ID set including descendants.
    """
    folder_ids: set[str] = {folder_id}
    queue: list[str] = [folder_id]

    while queue:
        current_folder_id = queue.pop(0)
        children = (
            db.query(Folder.id)
            .filter(Folder.user_id == user.id, Folder.parent_id == current_folder_id)
            .all()
        )
        for child_id, in children:
            if child_id not in folder_ids:
                folder_ids.add(child_id)
                queue.append(child_id)

    return folder_ids


def _build_tree(
    db: Session,
    user: User,
    dataset_id: str | None = None,
    folder_id: str | None = None,
) -> UploadsTreeResponse:
    """
    Construct the nested upload tree for a user, optionally filtered by dataset or folder.
    """
    if dataset_id:
        get_dataset_by_id(db, user, dataset_id)

    if folder_id:
        root_folder = _get_folder_by_id(db, user, folder_id)
        root = UploadsTreeResponse(id=root_folder.id, type="folder", name=root_folder.name, children=[])
        scoped_folder_ids = _collect_descendant_folder_ids(db, user, root_folder.id)
    else:
        root = UploadsTreeResponse(id=str(uuid4()), type="folder", name="root", children=[])
        scoped_folder_ids = set()

    mapping_query = (
        db.query(DatasetFolderFilesMapping)
        .filter(DatasetFolderFilesMapping.user_id == user.id)
    )
    if dataset_id:
        mapping_query = mapping_query.filter(DatasetFolderFilesMapping.dataset_id == dataset_id)
    if folder_id:
        mapping_query = mapping_query.filter(DatasetFolderFilesMapping.folder_id.in_(scoped_folder_ids))

    mappings = mapping_query.all()

    if folder_id:
        all_folder_ids = scoped_folder_ids
    else:
        folder_ids = {m.folder_id for m in mappings if m.folder_id is not None}
        all_folder_ids = set(folder_ids)
        for fid in list(folder_ids):
            curr_id = fid
            while curr_id:
                f = db.query(Folder).filter(Folder.id == curr_id, Folder.user_id == user.id).first()
                if f and f.parent_id:
                    all_folder_ids.add(f.parent_id)
                    curr_id = f.parent_id
                else:
                    break

    folders = (
        db.query(Folder)
        .filter(Folder.id.in_(all_folder_ids), Folder.user_id == user.id)
        .all()
        if all_folder_ids
        else []
    )

    folder_map: dict[str, UploadsTreeResponse] = {"root": root}
    for folder in folders:
        if folder_id and folder.id == root.id:
            folder_map[folder.id] = root
            continue
        node = UploadsTreeResponse(id=folder.id, type="folder", name=folder.name, children=[])
        folder_map[folder.id] = node

    for folder in folders:
        if folder_id and folder.id == root.id:
            continue

        parent_id = folder.parent_id if folder_id else (folder.parent_id or "root")
        if parent_id in folder_map:
            folder_map[parent_id].children = folder_map[parent_id].children or []
            folder_map[parent_id].children.append(folder_map[folder.id])

    for m in mappings:
        uploaded_file = db.query(UploadedFile).filter(UploadedFile.id == m.file_id).first()
        if not uploaded_file:
            continue
        parent_id = m.folder_id or "root"
        node = UploadsTreeResponse(
            id=uploaded_file.id,
            type="file",
            name=uploaded_file.filename,
            size=uploaded_file.file_size_bytes,
        )
        if parent_id in folder_map:
            folder_map[parent_id].children = folder_map[parent_id].children or []
            if not any(c.id == node.id for c in folder_map[parent_id].children):
                folder_map[parent_id].children.append(node)

    return root


def _compute_total_chunks(filesize: int) -> int:
    return ceil(filesize / CHUNK_SIZE_BYTES)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def initialize_upload(db: Session, user: User, payload: UploadInitRequest) -> UploadInitResponse:
    """
    Create an upload session and short-circuit when the file already exists.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        payload (UploadInitRequest): The upload initialization details.

    Returns:
        UploadInitResponse: The upload initialization details.

    Raises:
        HTTPException: If the dataset is not found, invalid, or unauthorized.
    """
    _ensure_storage_dirs()

    # Enforce mandatory dataset ownership check
    get_dataset_by_id(db, user, payload.dataset_id)

    folder = _get_folder_tree(db, user, payload.relative_path, payload.filename)
    if folder is not None:
        db.commit()

    # 1. Check if exact mapping already exists (duplicate_short_circuit)
    exists_mapping = (
        db.query(DatasetFolderFilesMapping)
        .join(UploadedFile)
        .filter(
            DatasetFolderFilesMapping.user_id == user.id,
            DatasetFolderFilesMapping.dataset_id == payload.dataset_id,
            DatasetFolderFilesMapping.folder_id == (folder.id if folder else None),
            UploadedFile.master_hash == payload.master_hash,
        )
        .first()
    )
    if exists_mapping is not None:
        return UploadInitResponse(
            upload_id=str(uuid4()),
            chunk_size=CHUNK_SIZE_BYTES,
            total_chunks=0,
            status="duplicate_short_circuit",
        )

    # 2. Check if physical file exists globally (duplicate_suspected)
    exists_file = (
        db.query(UploadedFile)
        .filter(UploadedFile.master_hash == payload.master_hash)
        .first()
    )

    upload_id = str(uuid4())

    if exists_file is not None:
        # Save upload session metadata in Redis without creating ghost SQL mappings
        redis_server.hset(
            _meta_key(upload_id),
            mapping={
                "user_id": user.id,
                "dataset_id": payload.dataset_id,
                "filename": payload.filename,
                "filesize": str(payload.filesize),
                "master_hash": payload.master_hash,
                "folder_id": folder.id if folder else "",
                "total_chunks": "0",
                "linked_file_id": exists_file.id,
            },
        )
        redis_server.expire(_meta_key(upload_id), SESSION_TTL_SECONDS)

        return UploadInitResponse(
            upload_id=upload_id,
            chunk_size=CHUNK_SIZE_BYTES,
            total_chunks=0,
            status="duplicate_suspected",
        )

    # 3. New file upload flow
    total_chunks = _compute_total_chunks(payload.filesize)
    _parts_dir(upload_id).mkdir(parents=True, exist_ok=True)

    redis_server.hset(
        _meta_key(upload_id),
        mapping={
            "user_id": user.id,
            "dataset_id": payload.dataset_id,
            "filename": payload.filename,
            "filesize": str(payload.filesize),
            "master_hash": payload.master_hash,
            "folder_id": folder.id if folder else "",
            "total_chunks": str(total_chunks),
        },
    )
    redis_server.expire(_meta_key(upload_id), SESSION_TTL_SECONDS)
    redis_server.expire(_bitmap_key(upload_id), SESSION_TTL_SECONDS)
    redis_server.expire(_chunk_hashes_key(upload_id), SESSION_TTL_SECONDS)

    return UploadInitResponse(
        upload_id=upload_id,
        chunk_size=CHUNK_SIZE_BYTES,
        total_chunks=total_chunks,
        status="created",
    )


def process_upload_chunk(db: Session, user: User, payload: UploadChunkRequest, chunk_bytes: bytes) -> UploadChunkResponse:
    """
    Process a single upload chunk atomically and lock-free.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        payload (UploadChunkRequest): The chunk payload details.
        chunk_bytes (bytes): The raw chunk bytes.

    Returns:
        UploadChunkResponse: Details of the processed chunk.

    Raises:
        HTTPException: If the session is not found, unauthorized, or hash matches fail.
    """
    meta = redis_server.hgetall(_meta_key(payload.upload_id))
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if meta.get("user_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload does not belong to the current user")

    total_chunks = int(meta["total_chunks"])
    if payload.chunk_index >= total_chunks or payload.chunk_index < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chunk index outside the valid range")

    computed_hash = _hash_bytes(chunk_bytes)
    if computed_hash != payload.chunk_hash:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chunk hash mismatch")

    bitmap_key = _bitmap_key(payload.upload_id)
    hashes_key = _chunk_hashes_key(payload.upload_id)
    meta_key = _meta_key(payload.upload_id)

    # Check and set chunk state atomically using the Lua script (0 = new, 1 = exists)
    already_uploaded = atomic_chunk_state(
        keys=[bitmap_key, hashes_key, meta_key],
        args=[payload.chunk_index, computed_hash, SESSION_TTL_SECONDS],
    )

    if already_uploaded == 0:
        # Write the chunk to its isolated file path (lock-free)
        chunk_file_path = _chunk_path(payload.upload_id, payload.chunk_index)
        chunk_file_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_file_path.write_bytes(chunk_bytes)

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


def finalize_upload(db: Session, user: User, payload: UploadFinalizeRequest) -> UploadFinalizeResponse:
    """
    Merge completed chunks into a flat physical file and create mappings.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        payload (UploadFinalizeRequest): The upload finalization details.

    Returns:
        UploadFinalizeResponse: The details of the finalized file mapping.

    Raises:
        HTTPException: If session is missing, unauthorized, incomplete, or hashes mismatch.
    """
    meta = redis_server.hgetall(_meta_key(payload.upload_id))
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")
    if meta.get("user_id") != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload does not belong to the current user")
    if meta.get("master_hash") != payload.master_hash:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="master_hash mismatch")

    dataset_id = meta["dataset_id"]
    folder_id = meta.get("folder_id") or None
    if folder_id == "":
        folder_id = None

    if folder_id is not None:
        folder = (
            db.query(Folder)
            .filter(Folder.id == folder_id, Folder.user_id == user.id)
            .one_or_none()
        )
        if folder is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    final_file_id = None

    # Check if this is a suspected duplicate fast-link session
    linked_file_id = meta.get("linked_file_id")
    if linked_file_id:
        # Verify the physical file exists
        uploaded_file = db.query(UploadedFile).filter(UploadedFile.id == linked_file_id).one_or_none()
        if uploaded_file is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked file not found")

        final_file_id = linked_file_id
    else:
        # Standard upload flow
        total_chunks = int(meta["total_chunks"])
        bitmap_key = _bitmap_key(payload.upload_id)
        received_chunks = int(redis_server.bitcount(bitmap_key) or 0)
        if received_chunks != total_chunks:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload is incomplete")

        # 1. Check if the physical file exists globally (concurrency safety)
        uploaded_file = (
            db.query(UploadedFile)
            .filter(UploadedFile.master_hash == payload.master_hash)
            .first()
        )

        if uploaded_file is not None:
            # The file already exists globally (e.g. uploaded concurrently)
            final_file_id = uploaded_file.id
            parts_dir = _parts_dir(payload.upload_id)
            shutil.rmtree(parts_dir, ignore_errors=True)
        else:
            # Perform chunk assembly
            parts_dir = _parts_dir(payload.upload_id)
            staging_file_id = str(uuid4())
            staging_path = FINAL_ROOT / f"{staging_file_id}.pending"
            try:
                with staging_path.open("wb") as destination:
                    for index in range(total_chunks):
                        chunk_file_path = _chunk_path(payload.upload_id, index)
                        if not chunk_file_path.exists():
                            staging_path.unlink(missing_ok=True)
                            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Missing chunk {index}")
                        with chunk_file_path.open("rb") as source:
                            shutil.copyfileobj(source, destination)

                final_file_id = str(uuid4())
                final_filename = _uploaded_filename(meta["filename"])
                final_path = FINAL_ROOT / final_filename
                if final_path.exists():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A file with this name already exists.",
                    )
                staging_path.replace(final_path)
                file_size_bytes = final_path.stat().st_size

                # Create physical file entry
                new_uploaded_file = UploadedFile(
                    id=final_file_id,
                    filename=meta["filename"],
                    file_size_bytes=file_size_bytes,
                    master_hash=meta["master_hash"],
                    physical_path=str(final_path),
                )
                db.add(new_uploaded_file)
                db.flush()
            except Exception:
                staging_path.unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(parts_dir, ignore_errors=True)

    # 2. Link file to the mapping using ON CONFLICT DO NOTHING
    stmt = (
        pg_insert(DatasetFolderFilesMapping)
        .values(
            dataset_id=dataset_id,
            folder_id=folder_id,
            file_id=final_file_id,
            user_id=user.id,
        )
        .on_conflict_do_nothing(
            constraint="uq_dataset_folder_file",
        )
    )
    db.execute(stmt)
    db.commit()

    # Clear Redis keys
    redis_server.delete(_meta_key(payload.upload_id))
    if not linked_file_id:
        redis_server.delete(_bitmap_key(payload.upload_id), _chunk_hashes_key(payload.upload_id))

    return UploadFinalizeResponse(status="success", file_id=final_file_id, folder_id=folder_id)


def delete_user_upload(db: Session, user: User, upload_id: str) -> UploadDeleteResponse:
    """Delete an uploaded file and its physical storage for the authenticated user."""
    uploaded_file = (
        db.query(UploadedFile)
        .filter(UploadedFile.id == upload_id, UploadedFile.user_id == user.id)
        .one_or_none()
    )
    if uploaded_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload not found")

    file_path = Path(uploaded_file.physical_path)
    if file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass

    db.delete(uploaded_file)
    db.commit()

    return UploadDeleteResponse(status="deleted", file_id=upload_id)


def list_user_uploads(
    db: Session,
    user: User,
    dataset_id: str | None = None,
    folder_id: str | None = None,
) -> UploadsTreeResponse:
    """
    Return the authenticated user's uploaded files as a nested tree.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        dataset_id (str | None): Optional dataset ID filter.
        folder_id (str | None): Optional folder ID filter.

    Returns:
        UploadsTreeResponse: The nested uploads tree.
    """
    return _build_tree(db, user, dataset_id=dataset_id, folder_id=folder_id)


def create_dataset(db: Session, user: User, payload: DatasetCreate) -> Dataset:
    """
    Create a new dataset catalog entry.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        payload (DatasetCreate): The dataset creation details.

    Returns:
        Dataset: The created dataset database record.

    Raises:
        HTTPException: If a dataset with the same name already exists.
    """
    existing = (
        db.query(Dataset)
        .filter(
            Dataset.user_id == user.id,
            func.lower(Dataset.name) == payload.name.strip().lower(),
            Dataset.is_deleted == False,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A dataset with this name already exists.",
        )

    dataset = Dataset(
        user_id=user.id,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        source_type=payload.source_type.strip() if payload.source_type else None,
        content_type=payload.content_type.strip() if payload.content_type else None,
        format=payload.format.strip() if payload.format else None,
        language=payload.language.strip() if payload.language else None,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def get_datasets(db: Session, user: User, page: int = 1, limit: int = 10) -> list[Dataset]:
    """
    Return all active datasets belonging to the user.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        page (int): The page number for pagination.
        limit (int): The number of datasets to return per page.

    Returns:
        list[Dataset]: The list of active dataset records.
    """
    offset = (page - 1) * limit
    return (
        db.query(Dataset)
        .filter(Dataset.user_id == user.id, Dataset.is_deleted == False)
        .order_by(Dataset.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def get_dataset_by_id(db: Session, user: User, dataset_id: str) -> Dataset:
    """
    Retrieve an active dataset by its ID and check ownership.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        dataset_id (str): The ID of the dataset to retrieve.

    Returns:
        Dataset: The active dataset database record.

    Raises:
        HTTPException: If the dataset is not found or does not belong to the user.
    """
    dataset = (
        db.query(Dataset)
        .filter(Dataset.id == dataset_id, Dataset.is_deleted == False)
        .one_or_none()
    )
    if dataset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dataset not found.",
        )
    if dataset.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dataset does not belong to the current user.",
        )
    return dataset


def update_dataset(db: Session, user: User, dataset_id: str, payload: DatasetUpdate) -> Dataset:
    """
    Update details of an active dataset.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        dataset_id (str): The ID of the dataset to update.
        payload (DatasetUpdate): The dataset fields to update.

    Returns:
        Dataset: The updated dataset database record.

    Raises:
        HTTPException: If the name is duplicate or update fails.
    """
    dataset = get_dataset_by_id(db, user, dataset_id)

    if payload.name is not None:
        name_clean = payload.name.strip()
        if not name_clean:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Dataset name cannot be empty.",
            )
        # Check uniqueness if name is changing
        if name_clean.lower() != dataset.name.lower():
            existing = (
                db.query(Dataset)
                .filter(
                    Dataset.user_id == user.id,
                    func.lower(Dataset.name) == name_clean.lower(),
                    Dataset.is_deleted == False,
                )
                .first()
            )
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A dataset with this name already exists.",
                )
        dataset.name = name_clean

    if payload.description is not None:
        dataset.description = payload.description.strip()

    if payload.source_type is not None:
        dataset.source_type = payload.source_type.strip()

    if payload.content_type is not None:
        dataset.content_type = payload.content_type.strip()

    if payload.format is not None:
        dataset.format = payload.format.strip()

    if payload.language is not None:
        dataset.language = payload.language.strip()

    dataset.updated_at = datetime.now()
    db.commit()
    db.refresh(dataset)
    return dataset


def delete_dataset(db: Session, user: User, dataset_id: str) -> None:
    """
    Soft-delete an active dataset if no files are attached.

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        dataset_id (str): The ID of the dataset to delete.

    Returns:
        None

    Raises:
        HTTPException: If files are attached to this dataset.
    """
    dataset = get_dataset_by_id(db, user, dataset_id)

    # Enforce soft-delete cascade rules: Conflict if files are attached
    attached_files_count = (
        db.query(DatasetFolderFilesMapping)
        .filter(DatasetFolderFilesMapping.dataset_id == dataset_id)
        .count()
    )
    if attached_files_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete dataset: files are attached.",
        )

    dataset.is_deleted = True
    db.commit()


def attach_file_to_dataset(
    db: Session,
    user: User,
    dataset_id: str,
    payload: DatasetAttachFileRequest,
) -> DatasetAttachFileResponse:
    """
    Attach an existing uploaded file to a dataset and optional folder (zero-I/O).

    Args:
        db (Session): The active database session.
        user (User): The authenticated user.
        dataset_id (str): The target dataset ID.
        payload (DatasetAttachFileRequest): The attachment request schema.

    Returns:
        DatasetAttachFileResponse: The attach operation result.

    Raises:
        HTTPException: If the dataset or file is not found, or IDOR ownership check fails.
    """
    # 1. Enforce dataset ownership
    dataset = get_dataset_by_id(db, user, dataset_id)

    # 2. Strict IDOR ownership check: user must own at least one mapping to this file_id
    user_file_mapping = (
        db.query(DatasetFolderFilesMapping)
        .filter(
            DatasetFolderFilesMapping.user_id == user.id,
            DatasetFolderFilesMapping.file_id == payload.file_id,
        )
        .first()
    )
    if user_file_mapping is None:
        file_exists = db.query(UploadedFile).filter(UploadedFile.id == payload.file_id).first()
        if file_exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")
        else:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="File does not belong to the user.")

    # 3. Resolve folder structure if relative_path provided
    folder = _get_folder_tree(db, user, payload.relative_path)
    folder_id = folder.id if folder else None

    # 4. Check if mapping already exists
    existing_mapping = (
        db.query(DatasetFolderFilesMapping)
        .filter(
            DatasetFolderFilesMapping.dataset_id == dataset.id,
            DatasetFolderFilesMapping.folder_id == folder_id,
            DatasetFolderFilesMapping.file_id == payload.file_id,
        )
        .first()
    )

    if existing_mapping is None:
        mapping = DatasetFolderFilesMapping(
            dataset_id=dataset.id,
            folder_id=folder_id,
            file_id=payload.file_id,
            user_id=user.id,
        )
        db.add(mapping)
        db.commit()

    return DatasetAttachFileResponse(
        status="attached",
        dataset_id=dataset.id,
        file_id=payload.file_id,
        folder_id=folder_id,
    )

