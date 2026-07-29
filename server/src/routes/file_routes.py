"""HTTP routes for file ingestion workflows."""

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, UploadFile, status
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
    UserIntegrationsResponse,
    IngestionJobStatusResponse,
    GDriveIngestInitRequest,
    GDriveIngestUrlRequest,
    GDriveIngestResponse,
    SharepointTreeResponse,
    SharepointIngestInitRequest,
    SharepointIngestUrlRequest,
    SharepointIngestResponse,
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
    get_dataset_tree_for_dataset,
    get_datasets,
    update_dataset,
    delete_dataset,
    attach_file_to_dataset,
    get_user_integrations,
    get_ingestion_job_status,
)
from services.gdrive_service import initiate_gdrive_ingestion, get_user_google_access_token
from services.sharepoint_service import get_sharepoint_tree, initiate_sharepoint_ingestion


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
    include_completed: bool = True,
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
    return get_datasets(db, user, page=page, limit=limit, include_completed=include_completed)


@file_router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
def get_dataset_by_id_route(
    dataset_id: str,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Return a single dataset by ID for the authenticated user, including its file/folder tree."""
    user = _resolve_current_user(authorization, db)
    dataset = get_dataset_by_id(db, user, dataset_id)
    dataset.tree = get_dataset_tree_for_dataset(db, user, dataset.id)
    return dataset


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


@file_router.get("/users/me/integrations", response_model=UserIntegrationsResponse)
def get_user_integrations_endpoint(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Return the authenticated user's cloud integrations connection status.

    Args:
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        UserIntegrationsResponse: The user's integration status flags.
    """
    user = _resolve_current_user(authorization, db)
    return get_user_integrations(user)


@file_router.get("/users/me/google-token")
def get_user_google_token_endpoint(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Obtain a fresh Google OAuth access token for Google Picker API initialization.
    """
    user = _resolve_current_user(authorization, db)
    token = get_user_google_access_token(user)
    return {"access_token": token}


@file_router.get("/ingest/status/{job_id}", response_model=IngestionJobStatusResponse)
def get_ingestion_job_status_endpoint(
    job_id: str,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Poll the status and progress of an asynchronous cloud ingestion job.

    Args:
        job_id (str): The unique cloud ingestion job ID.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        IngestionJobStatusResponse: Details of the job status and completion percentage.
    """
    user = _resolve_current_user(authorization, db)
    return get_ingestion_job_status(db, user, job_id)


@file_router.post("/ingest/gdrive/init", response_model=GDriveIngestResponse)
def ingest_gdrive_init_endpoint(
    payload: GDriveIngestInitRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a Google Drive file by file_id.

    Args:
        payload (GDriveIngestInitRequest): Request containing target dataset_id and file_id.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        GDriveIngestResponse: Acknowledgment payload with job_id and status.
    """
    user = _resolve_current_user(authorization, db)
    return initiate_gdrive_ingestion(
        db=db,
        user=user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.file_id,
        background_tasks=background_tasks,
        folder_id=payload.folder_id,
        filename=payload.filename,
        mime_type=payload.mime_type,
    )


@file_router.post("/ingest/gdrive/url", response_model=GDriveIngestResponse)
def ingest_gdrive_url_endpoint(
    payload: GDriveIngestUrlRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a Google Drive file by shared URL.

    Args:
        payload (GDriveIngestUrlRequest): Request containing target dataset_id and gdrive_url.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        GDriveIngestResponse: Acknowledgment payload with job_id and status.
    """
    user = _resolve_current_user(authorization, db)
    return initiate_gdrive_ingestion(
        db=db,
        user=user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.gdrive_url,
        background_tasks=background_tasks,
        folder_id=payload.folder_id,
        filename=payload.filename,
        mime_type=payload.mime_type,
    )


@file_router.get("/ingest/sharepoint/tree", response_model=SharepointTreeResponse)
def get_sharepoint_tree_endpoint(
    folder_id: str | None = None,
    drive_id: str | None = None,
    site_id: str | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Explore Microsoft SharePoint / Graph API directory tree and items.

    Args:
        folder_id (str | None): Optional target folder ID.
        drive_id (str | None): Optional target drive ID.
        site_id (str | None): Optional target site ID.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        SharepointTreeResponse: Directory listing containing folder and file items.
    """
    user = _resolve_current_user(authorization, db)
    return get_sharepoint_tree(
        db=db,
        user=user,
        folder_id=folder_id,
        drive_id=drive_id,
        site_id=site_id,
    )


@file_router.post("/ingest/sharepoint/init", response_model=SharepointIngestResponse)
def ingest_sharepoint_init_endpoint(
    payload: SharepointIngestInitRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a SharePoint file by item ID.

    Args:
        payload (SharepointIngestInitRequest): Request payload containing target dataset_id and file_id.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        SharepointIngestResponse: Acknowledgment payload with job_id and instant deduplication flag.
    """
    user = _resolve_current_user(authorization, db)
    return initiate_sharepoint_ingestion(
        db=db,
        user=user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.file_id,
        background_tasks=background_tasks,
        quick_xor_hash=payload.quick_xor_hash,
        folder_id=payload.folder_id,
        filename=payload.filename,
    )


@file_router.post("/ingest/sharepoint/url", response_model=SharepointIngestResponse)
def ingest_sharepoint_url_endpoint(
    payload: SharepointIngestUrlRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a SharePoint file by shared URL.

    Args:
        payload (SharepointIngestUrlRequest): Request payload containing target dataset_id and sharepoint_url.
        background_tasks (BackgroundTasks): FastAPI background task manager.
        authorization (str | None): Optional authorization bearer header.
        db (Session): The active database session.

    Returns:
        SharepointIngestResponse: Acknowledgment payload with job_id and instant deduplication flag.
    """
    user = _resolve_current_user(authorization, db)
    return initiate_sharepoint_ingestion(
        db=db,
        user=user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.sharepoint_url,
        background_tasks=background_tasks,
        quick_xor_hash=payload.quick_xor_hash,
        folder_id=payload.folder_id,
        filename=payload.filename,
    )



