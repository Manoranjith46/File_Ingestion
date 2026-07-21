"""HTTP routes for file ingestion workflows."""

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from config.database import get_db
from models.auth_model import User
from schemas.file_schema import (
    UploadChunkRequest,
    UploadChunkResponse,
    UploadDeleteRequest,
    UploadDeleteResponse,
    UploadFinalizeRequest,
    UploadFinalizeResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadsTreeResponse,
    DatasetCreate,
    DatasetUpdate,
    DatasetResponse,
    DatasetAttachFileRequest,
    DatasetAttachFileResponse,
)
from services.auth_services import get_current_user
from services.file_services import (
    delete_user_upload,
    finalize_upload,
    initialize_upload,
    list_user_uploads,
    process_upload_chunk,
    create_dataset,
    get_dataset_by_id,
    get_datasets,
    update_dataset,
    delete_dataset,
    attach_file_to_dataset,
)


file_router = APIRouter()


def _resolve_current_user(authorization: str | None, db: Session) -> User:
    """Resolve the current user from a bearer token."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")
    return get_current_user(db, authorization.removeprefix("Bearer ").strip())


@file_router.post("/upload/init", response_model=UploadInitResponse)
def upload_init(
    payload: UploadInitRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Initialize a chunked file upload session."""
    user = _resolve_current_user(authorization, db)
    return initialize_upload(db, user, payload)


@file_router.post("/upload/chunk", response_model=UploadChunkResponse)
def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(..., ge=0),
    chunk_hash: str = Form(..., min_length=64, max_length=64),
    authorization: str | None = Header(default=None, alias="Authorization"),
    chunk_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Process a single uploaded chunk."""
    user = _resolve_current_user(authorization, db)
    payload = UploadChunkRequest(upload_id=upload_id, chunk_index=chunk_index, chunk_hash=chunk_hash)
    chunk_bytes = chunk_file.file.read()
    return process_upload_chunk(db, user, payload, chunk_bytes)


@file_router.post("/upload/finalize", response_model=UploadFinalizeResponse)
def upload_finalize(
    payload: UploadFinalizeRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Finalize a completed upload session and persist the uploaded file."""
    user = _resolve_current_user(authorization, db)
    return finalize_upload(db, user, payload)


@file_router.get("/uploads", response_model=UploadsTreeResponse)
def uploads(
    dataset_id: str | None = None,
    folder_id: str | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's upload tree with optional dataset and folder filtering."""
    user = _resolve_current_user(authorization, db)
    return list_user_uploads(db, user, dataset_id=dataset_id, folder_id=folder_id)


@file_router.post("/uploads/delete", response_model=UploadDeleteResponse)
def delete_upload(
    payload: UploadDeleteRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Delete a single upload belonging to the authenticated user."""
    user = _resolve_current_user(authorization, db)
    return delete_user_upload(db, user, payload.upload_id)


@file_router.post("/datasets", response_model=DatasetResponse, status_code=status.HTTP_201_CREATED)
def create_new_dataset(
    payload: DatasetCreate,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Create a new dataset under the authenticated user's account.

    Args:
        payload (DatasetCreate): The dataset schema payload.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        DatasetResponse: The created dataset details.
    """
    user = _resolve_current_user(authorization, db)
    return create_dataset(db, user, payload)


@file_router.get("/datasets", response_model=list[DatasetResponse])
def list_datasets(
    page: int = 1,
    limit: int = 10,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    List all active datasets under the authenticated user's account.

    Args:
        page (int): Page number for pagination.
        limit (int): Maximum records per page.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        list[DatasetResponse]: A list of dataset details.
    """
    user = _resolve_current_user(authorization, db)
    return get_datasets(db, user, page=page, limit=limit)


@file_router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
def get_dataset_by_id_route(
    dataset_id: str,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Return a single dataset by ID for the authenticated user."""
    user = _resolve_current_user(authorization, db)
    return get_dataset_by_id(db, user, dataset_id)


@file_router.patch("/datasets/{dataset_id}", response_model=DatasetResponse)
def modify_dataset(
    dataset_id: str,
    payload: DatasetUpdate,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Modify metadata of an existing dataset.

    Args:
        dataset_id (str): The unique dataset ID.
        payload (DatasetUpdate): The patch updates schema.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        DatasetResponse: The updated dataset details.
    """
    user = _resolve_current_user(authorization, db)
    return update_dataset(db, user, dataset_id, payload)


@file_router.delete("/datasets/{dataset_id}", status_code=status.HTTP_200_OK)
def remove_dataset(
    dataset_id: str,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Soft-delete a dataset if no files are currently attached.

    Args:
        dataset_id (str): The unique dataset ID.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        dict: Confirmation payload of the deletion.
    """
    user = _resolve_current_user(authorization, db)
    delete_dataset(db, user, dataset_id)
    return {"status": "deleted", "dataset_id": dataset_id}


@file_router.post("/datasets/{dataset_id}/files", response_model=DatasetAttachFileResponse)
def attach_file(
    dataset_id: str,
    payload: DatasetAttachFileRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Attach an existing uploaded file to a dataset and optional folder (zero-I/O).

    Args:
        dataset_id (str): Target dataset ID.
        payload (DatasetAttachFileRequest): File attachment details.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        DatasetAttachFileResponse: The attach result schema.
    """
    user = _resolve_current_user(authorization, db)
    return attach_file_to_dataset(db, user, dataset_id, payload)
