"""Business logic for chunked file upload workflows."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from math import ceil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from config.redis_server import server as redis_server, atomic_chunk_state
from helpers.get_env import get_env
from models.auth_model import User
from models.file_model import Dataset, IngestedFile, IngestedFileProviderType
from schemas.file_schema import (
    CHUNK_SIZE_BYTES,
    UploadDeleteResponse,
    UploadFinalizeRequest,
    UploadFinalizeResponse,
    UploadChunkRequest,
    UploadChunkResponse,
    UploadInitRequest,
    UploadInitResponse,
    UserIntegrationsResponse,
    IngestionJobStatusResponse,
    UploadsTreeResponse,
    DatasetCreate,
    DatasetUpdate,
    DatasetAttachFileRequest,
    DatasetAttachFileResponse,
)
from utils.errors import (
    ChunkValidationError,
    DatasetAccessError,
    DatasetNotFoundError,
    DuplicateDatasetError,
    FileNotFoundError,
    FileOwnershipError,
    FilenameCollisionError,
    IngestionJobNotFoundError,
    InvalidDatasetStateError,
    InvalidRelativePathError,
    MissingChunkError,
    UploadIncompleteError,
    UploadNotFoundError,
    UploadOwnershipError,
    UploadSessionNotFoundError,
)

UPLOAD_ROOT = Path(get_env("UPLOAD_STORAGE_DIR", default=str(Path(__file__).resolve().parents[2] / "uploads"), required=False))
PARTS_ROOT = UPLOAD_ROOT / ".parts"
FINAL_ROOT = UPLOAD_ROOT / "files"
LOCAL_STORAGE_DIR = FINAL_ROOT / "Local"
GDRIVE_STORAGE_DIR = FINAL_ROOT / "GDrive"
SHAREPOINT_STORAGE_DIR = FINAL_ROOT / "Sharepoint"
SESSION_TTL_SECONDS = int(get_env("UPLOAD_SESSION_TTL_SECONDS", default="3600", required=False))


def _ensure_storage_dirs() -> None:
    """Ensure upload storage directories exist for each provider."""
    PARTS_ROOT.mkdir(parents=True, exist_ok=True)
    FINAL_ROOT.mkdir(parents=True, exist_ok=True)
    LOCAL_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    GDRIVE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    SHAREPOINT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def get_storage_root_for_provider(source_type: str | IngestedFileProviderType | None) -> Path:
    """Return the designated storage directory for a file based on its source type / provider."""
    if isinstance(source_type, IngestedFileProviderType):
        val = source_type.value
    else:
        val = str(source_type) if source_type else "Local"

    val_lower = val.lower()
    if "gdrive" in val_lower or "google" in val_lower:
        dir_path = GDRIVE_STORAGE_DIR
    elif "sharepoint" in val_lower or "onedrive" in val_lower or "microsoft" in val_lower:
        dir_path = SHAREPOINT_STORAGE_DIR
    elif "ftp" in val_lower:
        dir_path = FINAL_ROOT / "FTP"
    else:
        dir_path = LOCAL_STORAGE_DIR

    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


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


def _generate_rename_suggestion(filename: str, existing_names: set[str]) -> str:
    """Generate a non-colliding filename by appending (1), (2), etc."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        candidate = f"{stem} ({counter}){suffix}"
        if candidate not in existing_names:
            return candidate
        counter += 1


def _get_unique_filename(base_dir: Path, filename: str) -> Path:
    """Return a non-colliding Path by appending (1), (2), etc. if the file already exists."""
    clean_name = _uploaded_filename(filename)
    candidate = base_dir / clean_name
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


def _validate_relative_path(relative_path: str | None) -> list[str]:
    if relative_path is None or relative_path.strip() == "":
        return []

    normalized = relative_path.replace("\\", "/").strip()
    if normalized.startswith("/"):
        raise InvalidRelativePathError(message="relative_path must be relative")

    parts = [segment.strip() for segment in normalized.split("/") if segment.strip() and segment != "."]
    if any(part == ".." for part in parts):
        raise InvalidRelativePathError(message="relative_path must not contain traversal segments")

    return parts


def _resolve_folder_parts(relative_path: str | None, filename: str | None = None) -> list[str]:
    """Resolve the folder segments from an upload path."""
    parts = _validate_relative_path(relative_path)
    if not parts:
        return []

    if filename is not None and parts[-1] == Path(filename).name:
        return parts[:-1]

    return parts


def _build_tree(
    db: Session,
    user: User,
    dataset_id: str | None = None,
    folder_id: str | None = None,
) -> UploadsTreeResponse:
    """Construct the nested upload tree for a user."""
    dataset = None
    if dataset_id:
        try:
            dataset = get_dataset_by_id(db, user, dataset_id)
        except DatasetNotFoundError:
            return UploadsTreeResponse(id=str(uuid4()), type="folder", name="root", children=[])

    root = UploadsTreeResponse(
        id=str(uuid4()),
        type="folder",
        name="root",
        dataset_name=dataset.name if dataset else None,
        status=dataset.status if dataset else None,
        children=[],
    )

    query = db.query(IngestedFile).filter(IngestedFile.user_id == user.id)
    if dataset_id:
        query = query.filter(IngestedFile.dataset_id == dataset_id)
    files = query.all()

    folder_nodes: dict[str, UploadsTreeResponse] = {"root": root}

    for f in files:
        dataset_name = dataset.name if dataset else (f.dataset.name if f.dataset else None)
        dataset_status = dataset.status if dataset else (f.dataset.status if f.dataset else None)
        provider_val = f.provider.value if isinstance(f.provider, IngestedFileProviderType) else str(f.provider)

        file_node = UploadsTreeResponse(
            id=f.id,
            type="file",
            name=f.filename,
            size=f.file_size_bytes,
            dataset_name=dataset_name,
            status=dataset_status,
            source_type=provider_val,
        )

        parts = _resolve_folder_parts(f.file_path, f.filename)
        current_node = root
        current_path_acc = ""
        for part in parts:
            current_path_acc = f"{current_path_acc}/{part}"
            if current_path_acc not in folder_nodes:
                folder_node = UploadsTreeResponse(
                    id=str(uuid4()),
                    type="folder",
                    name=part,
                    dataset_name=dataset_name,
                    status=dataset_status,
                    children=[],
                )
                folder_nodes[current_path_acc] = folder_node
                current_node.children = current_node.children or []
                current_node.children.append(folder_node)
            current_node = folder_nodes[current_path_acc]

        current_node.children = current_node.children or []
        if not any(c.id == file_node.id for c in current_node.children):
            current_node.children.append(file_node)

    return root


def _compute_total_chunks(filesize: int) -> int:
    return ceil(filesize / CHUNK_SIZE_BYTES)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _merge_chunk_files(chunk_paths: list[Path], destination_path: Path) -> None:
    """Merge a sequence of chunk files into a single destination file."""
    with destination_path.open("wb") as destination:
        for chunk_path in chunk_paths:
            if not chunk_path.exists():
                destination.close()
                destination_path.unlink(missing_ok=True)
                raise MissingChunkError(message=f"Missing chunk {chunk_path.name}")
            with chunk_path.open("rb") as source:
                shutil.copyfileobj(source, destination)


def initialize_upload(db: Session, user: User, payload: UploadInitRequest) -> UploadInitResponse:
    """Create an upload session and short-circuit when the file already exists."""
    _ensure_storage_dirs()

    dataset = get_dataset_by_id(db, user, payload.dataset_id)
    if dataset.status == "Completed":
        raise InvalidDatasetStateError(message="Completed datasets cannot receive new uploads")

    # 1. Check if exact mapping already exists (duplicate_short_circuit)
    exists_file = (
        db.query(IngestedFile)
        .filter(
            IngestedFile.user_id == user.id,
            IngestedFile.dataset_id == payload.dataset_id,
            IngestedFile.master_hash == payload.master_hash,
        )
        .first()
    )
    if exists_file is not None:
        return UploadInitResponse(
            upload_id=str(uuid4()),
            chunk_size=CHUNK_SIZE_BYTES,
            total_chunks=0,
            status="duplicate_short_circuit",
        )

    # 2. Virtual Deduplication — filename collision check
    existing_files = (
        db.query(IngestedFile)
        .filter(
            IngestedFile.dataset_id == payload.dataset_id,
        )
        .all()
    )
    existing_names: set[str] = {
        f.filename for f in existing_files if f.master_hash != payload.master_hash
    }

    effective_filename = payload.filename
    if payload.filename in existing_names:
        suggestion = _generate_rename_suggestion(payload.filename, existing_names)
        if not payload.auto_rename:
            raise FilenameCollisionError(
                message="Filename collision detected",
                details={
                    "conflict_type": "name_exists",
                    "conflicting_files": [payload.filename],
                    "suggestion": suggestion,
                },
            )
        effective_filename = suggestion

    # 3. Check if physical file exists globally (duplicate_suspected)
    global_file = (
        db.query(IngestedFile)
        .filter(IngestedFile.master_hash == payload.master_hash)
        .first()
    )

    upload_id = str(uuid4())
    provider_str = payload.source_type or "Local"

    if global_file is not None:
        redis_server.hset(
            _meta_key(upload_id),
            mapping={
                "user_id": user.id,
                "dataset_id": payload.dataset_id,
                "filename": effective_filename,
                "filesize": str(payload.filesize),
                "master_hash": payload.master_hash,
                "relative_path": payload.relative_path or "",
                "total_chunks": "0",
                "linked_file_id": global_file.id,
                "source_type": provider_str,
            },
        )
        redis_server.expire(_meta_key(upload_id), SESSION_TTL_SECONDS)

        return UploadInitResponse(
            upload_id=upload_id,
            chunk_size=CHUNK_SIZE_BYTES,
            total_chunks=0,
            status="duplicate_suspected",
        )

    # 4. New file upload flow
    total_chunks = _compute_total_chunks(payload.filesize)
    _parts_dir(upload_id).mkdir(parents=True, exist_ok=True)

    redis_server.hset(
        _meta_key(upload_id),
        mapping={
            "user_id": user.id,
            "dataset_id": payload.dataset_id,
            "filename": effective_filename,
            "filesize": str(payload.filesize),
            "master_hash": payload.master_hash,
            "relative_path": payload.relative_path or "",
            "total_chunks": str(total_chunks),
            "source_type": provider_str,
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
    """Process a single upload chunk atomically and lock-free."""
    meta = redis_server.hgetall(_meta_key(payload.upload_id))
    if not meta:
        raise UploadSessionNotFoundError()
    if meta.get("user_id") != user.id:
        raise UploadOwnershipError()

    total_chunks = int(meta["total_chunks"])
    if payload.chunk_index >= total_chunks or payload.chunk_index < 0:
        raise ChunkValidationError(message="Chunk index outside the valid range")

    computed_hash = _hash_bytes(chunk_bytes)
    if computed_hash != payload.chunk_hash:
        raise ChunkValidationError(message="Chunk hash mismatch")

    bitmap_key = _bitmap_key(payload.upload_id)
    hashes_key = _chunk_hashes_key(payload.upload_id)
    meta_key = _meta_key(payload.upload_id)

    already_uploaded = atomic_chunk_state(
        keys=[bitmap_key, hashes_key, meta_key],
        args=[payload.chunk_index, computed_hash, SESSION_TTL_SECONDS],
    )

    if already_uploaded == 0:
        chunk_file_path = _chunk_path(payload.upload_id, payload.chunk_index)
        chunk_file_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_file_path.write_bytes(chunk_bytes)

    received_chunks = int(redis_server.bitcount(bitmap_key) or 0)
    return UploadChunkResponse(
        status="In Progress",
        upload_id=payload.upload_id,
        chunk_index=payload.chunk_index,
        bytes_received=len(chunk_bytes),
        received_chunks=received_chunks,
        total_chunks=total_chunks,
        complete=received_chunks == total_chunks,
    )


def finalize_upload(db: Session, user: User, payload: UploadFinalizeRequest) -> UploadFinalizeResponse:
    """Merge completed chunks into a flat physical file and create IngestedFile record."""
    meta = redis_server.hgetall(_meta_key(payload.upload_id))
    if not meta:
        raise UploadSessionNotFoundError()
    if meta.get("user_id") != user.id:
        raise UploadOwnershipError()
    if meta.get("master_hash") != payload.master_hash:
        raise ChunkValidationError(message="master_hash mismatch")

    dataset_id = meta["dataset_id"]
    source_type_value = meta.get("source_type") or "Local"
    relative_path = meta.get("relative_path") or meta["filename"]

    try:
        provider_enum = IngestedFileProviderType(source_type_value)
    except ValueError:
        provider_enum = IngestedFileProviderType.Local

    final_file_id = None
    linked_file_id = meta.get("linked_file_id")

    if linked_file_id:
        existing_file = db.query(IngestedFile).filter(IngestedFile.id == linked_file_id).one_or_none()
        if existing_file is None:
            raise FileNotFoundError(message="Linked file not found")

        final_file_id = str(uuid4())
        new_file = IngestedFile(
            id=final_file_id,
            user_id=user.id,
            dataset_id=dataset_id,
            provider=provider_enum,
            file_path=relative_path if relative_path else meta["filename"],
            filename=meta["filename"],
            physical_path=existing_file.physical_path,
            file_size_bytes=existing_file.file_size_bytes,
            master_hash=payload.master_hash,
            status="completed",
        )
        db.add(new_file)
        db.flush()
    else:
        total_chunks = int(meta["total_chunks"])
        bitmap_key = _bitmap_key(payload.upload_id)
        received_chunks = int(redis_server.bitcount(bitmap_key) or 0)
        if received_chunks != total_chunks:
            raise UploadIncompleteError()

        global_file = (
            db.query(IngestedFile)
            .filter(IngestedFile.master_hash == payload.master_hash)
            .first()
        )
        parts_dir = _parts_dir(payload.upload_id)

        if global_file is not None:
            final_file_id = str(uuid4())
            new_file = IngestedFile(
                id=final_file_id,
                user_id=user.id,
                dataset_id=dataset_id,
                provider=provider_enum,
                file_path=relative_path if relative_path else meta["filename"],
                filename=meta["filename"],
                physical_path=global_file.physical_path,
                file_size_bytes=global_file.file_size_bytes,
                master_hash=payload.master_hash,
                status="completed",
            )
            db.add(new_file)
            db.flush()
            shutil.rmtree(parts_dir, ignore_errors=True)
        else:
            target_root = get_storage_root_for_provider(provider_enum)
            staging_file_id = str(uuid4())
            staging_path = target_root / f"{staging_file_id}.pending"
            try:
                chunk_paths = [_chunk_path(payload.upload_id, index) for index in range(total_chunks)]
                _merge_chunk_files(chunk_paths, staging_path)

                final_file_id = str(uuid4())
                final_path = _get_unique_filename(target_root, meta["filename"])
                staging_path.replace(final_path)
                file_size_bytes = final_path.stat().st_size

                new_file = IngestedFile(
                    id=final_file_id,
                    user_id=user.id,
                    dataset_id=dataset_id,
                    provider=provider_enum,
                    file_path=relative_path if relative_path else meta["filename"],
                    filename=meta["filename"],
                    physical_path=str(final_path),
                    file_size_bytes=file_size_bytes,
                    master_hash=meta["master_hash"],
                    status="completed",
                )
                db.add(new_file)
                db.flush()
            except Exception:
                staging_path.unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(parts_dir, ignore_errors=True)

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one_or_none()
    if dataset is not None:
        _sync_dataset_status_from_mappings(db, dataset)

    db.commit()

    redis_server.delete(_meta_key(payload.upload_id))
    if not linked_file_id:
        redis_server.delete(_bitmap_key(payload.upload_id), _chunk_hashes_key(payload.upload_id))

    return UploadFinalizeResponse(status="completed", file_id=final_file_id, folder_id=None)


def delete_user_upload(db: Session, user: User, upload_id: str) -> UploadDeleteResponse:
    """Delete an uploaded file and its physical storage for the authenticated user."""
    ingested_file = db.query(IngestedFile).filter(IngestedFile.id == upload_id).one_or_none()
    if ingested_file is None:
        raise UploadNotFoundError()
    if ingested_file.user_id != user.id:
        raise UploadOwnershipError()

    dataset_id = ingested_file.dataset_id

    if ingested_file.physical_path:
        ref_count = (
            db.query(IngestedFile)
            .filter(IngestedFile.physical_path == ingested_file.physical_path, IngestedFile.id != upload_id)
            .count()
        )
        if ref_count == 0:
            p = Path(ingested_file.physical_path)
            if p.exists():
                p.unlink(missing_ok=True)

    db.delete(ingested_file)

    if dataset_id:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one_or_none()
        if dataset is not None:
            _sync_dataset_status_from_mappings(db, dataset)

    db.commit()
    return UploadDeleteResponse(status="deleted", file_id=upload_id)


def list_user_uploads(
    db: Session,
    user: User,
    dataset_id: str | None = None,
    folder_id: str | None = None,
) -> UploadsTreeResponse:
    """Return the authenticated user's uploaded files as a nested tree."""
    return _build_tree(db, user, dataset_id=dataset_id, folder_id=folder_id)


def _normalize_dataset_status(value: str | None) -> str:
    """Normalize dataset status values to the supported lifecycle states."""
    if value is None:
        return "Created"
    normalized = value.strip()
    lifecycle_map = {
        "created": "Created",
        "draft": "Draft",
        "completed": "Completed",
        "in progress": "Draft",
    }
    return lifecycle_map.get(normalized.lower(), "Created")


def _sync_dataset_status_from_mappings(db: Session, dataset: Dataset) -> None:
    """Update dataset status based on the current number of linked files."""
    if dataset.status == "Completed":
        return

    file_count = (
        db.query(IngestedFile)
        .filter(IngestedFile.dataset_id == dataset.id)
        .count()
    )

    dataset.status = "Draft" if file_count > 0 else "Created"


def create_dataset(db: Session, user: User, payload: DatasetCreate) -> Dataset:
    """Create a new dataset catalog entry."""
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
        raise DuplicateDatasetError()

    dataset = Dataset(
        user_id=user.id,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        status="Created",
        language=payload.language.strip(),
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def get_datasets(
    db: Session,
    user: User,
    page: int = 1,
    limit: int = 10,
    include_completed: bool = True,
) -> list[Dataset]:
    """Return all active datasets belonging to the user."""
    offset = (page - 1) * limit
    query = db.query(Dataset).filter(Dataset.user_id == user.id, Dataset.is_deleted == False)
    if not include_completed:
        query = query.filter(Dataset.status != "Completed")
    return (
        query.order_by(Dataset.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def get_dataset_tree_for_dataset(db: Session, user: User, dataset_id: str) -> UploadsTreeResponse:
    """Return the nested tree for all files and folders linked to a dataset."""
    return _build_tree(db, user, dataset_id=dataset_id, folder_id=None)


def get_dataset_by_id(db: Session, user: User, dataset_id: str) -> Dataset:
    """Retrieve an active dataset by its ID and check ownership."""
    dataset = (
        db.query(Dataset)
        .filter(Dataset.id == dataset_id, Dataset.is_deleted == False)
        .one_or_none()
    )
    if dataset is None:
        raise DatasetNotFoundError()
    if dataset.user_id != user.id:
        raise DatasetAccessError()
    return dataset


def update_dataset(db: Session, user: User, dataset_id: str, payload: DatasetUpdate) -> Dataset:
    """Update details of an active dataset."""
    dataset = get_dataset_by_id(db, user, dataset_id)

    if payload.name is not None:
        name_clean = payload.name.strip()
        if not name_clean:
            raise InvalidDatasetStateError(message="Dataset name cannot be empty.")
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
                raise DuplicateDatasetError()
        dataset.name = name_clean

    if payload.description is not None:
        dataset.description = payload.description.strip()

    if dataset.status == "Completed":
        raise InvalidDatasetStateError(message="Completed datasets cannot be modified.")

    if payload.target_dataset_id is not None:
        if payload.target_dataset_id == dataset_id:
            raise InvalidDatasetStateError(message="Target dataset must be different from the source dataset.")

        if dataset.status not in {"Created", "Draft"}:
            raise InvalidDatasetStateError(message="Source dataset must be in created or Draft status.")

        target_dataset = get_dataset_by_id(db, user, payload.target_dataset_id)
        if target_dataset.status not in {"Created", "Draft"}:
            raise InvalidDatasetStateError(message="Target dataset must be in created or Draft status.")

        query = db.query(IngestedFile).filter(
            IngestedFile.dataset_id == dataset.id,
            IngestedFile.user_id == user.id,
        )
        if payload.file_id is not None and payload.file_id.strip() != "":
            query = query.filter(IngestedFile.id == payload.file_id)

        files_to_move = query.all()
        for f in files_to_move:
            f.dataset_id = target_dataset.id

        dataset.status = "Draft"
        _sync_dataset_status_from_mappings(db, dataset)
        _sync_dataset_status_from_mappings(db, target_dataset)

    if payload.status is not None:
        requested_status = _normalize_dataset_status(payload.status)
        current_status = _normalize_dataset_status(dataset.status)
        allowed_transitions = {
            "Created": {"Draft", "Completed"},
            "Draft": {"Completed"},
            "Completed": set(),
        }
        if requested_status not in allowed_transitions.get(current_status, set()):
            raise InvalidDatasetStateError(message="Invalid dataset status transition.")
        dataset.status = requested_status

    if payload.language is not None:
        dataset.language = payload.language.strip()

    dataset.updated_at = datetime.now()
    db.commit()
    db.refresh(dataset)
    return dataset


def delete_dataset(db: Session, user: User, dataset_id: str) -> None:
    """Remove a dataset and all of its linked files."""
    dataset = get_dataset_by_id(db, user, dataset_id)

    files = (
        db.query(IngestedFile)
        .filter(IngestedFile.dataset_id == dataset_id)
        .all()
    )

    for f in files:
        if f.physical_path:
            ref_count = (
                db.query(IngestedFile)
                .filter(IngestedFile.physical_path == f.physical_path, IngestedFile.id != f.id)
                .count()
            )
            if ref_count == 0:
                p = Path(f.physical_path)
                if p.exists():
                    p.unlink(missing_ok=True)
        db.delete(f)

    db.delete(dataset)
    db.commit()


def attach_file_to_dataset(
    db: Session,
    user: User,
    dataset_id: str,
    payload: DatasetAttachFileRequest,
) -> DatasetAttachFileResponse:
    """Attach an existing uploaded file to a dataset."""
    dataset = get_dataset_by_id(db, user, dataset_id)
    if dataset.status == "Completed":
        raise InvalidDatasetStateError(message="Completed datasets cannot be modified.")

    user_file = (
        db.query(IngestedFile)
        .filter(IngestedFile.id == payload.file_id)
        .first()
    )
    if user_file is None:
        raise FileNotFoundError()
    if user_file.user_id != user.id:
        raise FileOwnershipError()

    relative_path = payload.relative_path or user_file.file_path

    existing_attach = (
        db.query(IngestedFile)
        .filter(
            IngestedFile.dataset_id == dataset.id,
            IngestedFile.user_id == user.id,
            IngestedFile.master_hash == user_file.master_hash,
            IngestedFile.file_path == relative_path,
        )
        .first()
    )

    if existing_attach is None:
        new_attach = IngestedFile(
            user_id=user.id,
            dataset_id=dataset.id,
            provider=user_file.provider,
            file_path=relative_path,
            filename=user_file.filename,
            physical_path=user_file.physical_path,
            file_size_bytes=user_file.file_size_bytes,
            master_hash=user_file.master_hash,
            provider_hash=user_file.provider_hash,
            status="completed",
        )
        db.add(new_attach)

    _sync_dataset_status_from_mappings(db, dataset)
    db.commit()

    return DatasetAttachFileResponse(
        status="attached",
        dataset_id=dataset.id,
        file_id=payload.file_id,
        folder_id=None,
    )


def get_user_integrations(user: User) -> UserIntegrationsResponse:
    """Retrieve the current cloud integration connection status for a user."""
    google_connected = bool(user.google_refresh_token and user.google_refresh_token.strip())
    microsoft_connected = bool(user.microsoft_refresh_token and user.microsoft_refresh_token.strip())
    return UserIntegrationsResponse(
        google_connected=google_connected,
        microsoft_connected=microsoft_connected,
    )


def get_ingestion_job_status(db: Session, user: User, job_id: str) -> IngestionJobStatusResponse:
    """Query the status and live progress of a background cloud ingestion job."""
    job = (
        db.query(IngestedFile)
        .filter(IngestedFile.id == job_id, IngestedFile.user_id == user.id)
        .one_or_none()
    )
    if job is None:
        raise IngestionJobNotFoundError()

    progress_val = redis_server.get(f"ingest:{job_id}:progress")
    progress_pct = 0
    if progress_val is not None:
        try:
            progress_pct = int(progress_val)
        except (ValueError, TypeError):
            pass
    elif job.status == "completed":
        progress_pct = 100

    provider_str = job.provider.value if isinstance(job.provider, IngestedFileProviderType) else str(job.provider)

    return IngestionJobStatusResponse(
        job_id=job.id,
        provider=provider_str,
        filename=job.filename,
        status=job.status,
        progress_percentage=progress_pct,
        error_message=job.error_message,
        file_id=job.id if job.status == "completed" else None,
        master_hash=job.master_hash,
    )
