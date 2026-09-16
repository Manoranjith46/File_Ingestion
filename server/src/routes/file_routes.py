"""HTTP routes for file ingestion workflows."""

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
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
from middlewares.tenant_context import TenantContext, get_tenant_context
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
from services.sharepoint_service import initiate_sharepoint_ingestion, get_sharepoint_tree


file_router = APIRouter()


@file_router.post("/upload/init", response_model=UploadInitResponse)
def upload_init(
    payload: UploadInitRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Initialize a chunked file upload session scoped to tenant."""
    return initialize_upload(db, tenant.user, payload, organization_id=tenant.organization_id)


@file_router.post("/upload/chunk", response_model=UploadChunkResponse)
def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(..., ge=0),
    chunk_hash: str = Form(..., min_length=64, max_length=64),
    tenant: TenantContext = Depends(get_tenant_context),
    chunk_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Process a single uploaded chunk."""
    payload = UploadChunkRequest(upload_id=upload_id, chunk_index=chunk_index, chunk_hash=chunk_hash)
    chunk_bytes = chunk_file.file.read()
    return process_upload_chunk(db, tenant.user, payload, chunk_bytes)


@file_router.post("/upload/finalize", response_model=UploadFinalizeResponse)
def upload_finalize(
    payload: UploadFinalizeRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Finalize a completed upload session and persist the uploaded file."""
    return finalize_upload(db, tenant.user, payload)


@file_router.get("/uploads", response_model=UploadsTreeResponse)
def uploads(
    dataset_id: str | None = None,
    folder_id: str | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's upload tree with optional dataset and folder filtering."""
    return list_user_uploads(db, tenant.user, dataset_id=dataset_id, folder_id=folder_id, organization_id=tenant.organization_id)


@file_router.post("/uploads/delete", response_model=UploadDeleteResponse)
def delete_upload(
    payload: UploadDeleteRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Delete a single upload belonging to the authenticated tenant."""
    return delete_user_upload(db, tenant.user, payload.upload_id, organization_id=tenant.organization_id)


@file_router.post("/datasets", response_model=DatasetResponse, status_code=status.HTTP_201_CREATED)
def create_new_dataset(
    payload: DatasetCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Create a new dataset under the authenticated user's account and active organization.

    Args:
        payload (DatasetCreate): The dataset schema payload.
        tenant (TenantContext): Resolved tenant and user context.
        db (Session): The active database session.

    Returns:
        DatasetResponse: The created dataset details.
    """
    return create_dataset(db, tenant.user, payload, organization_id=tenant.organization_id)


@file_router.get("/datasets", response_model=list[DatasetResponse])
def list_datasets(
    page: int = 1,
    limit: int = 10,
    include_completed: bool = True,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    List all active datasets under the active organization.

    Args:
        page (int): Page number for pagination.
        limit (int): Maximum records per page.
        include_completed (bool): Whether to include completed datasets.
        tenant (TenantContext): Resolved tenant and user context.
        db (Session): The active database session.

    Returns:
        list[DatasetResponse]: A list of dataset details.
    """
    return get_datasets(db, tenant.user, page=page, limit=limit, include_completed=include_completed, organization_id=tenant.organization_id)


@file_router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
def get_dataset_by_id_route(
    dataset_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """Return a single dataset by ID for the active organization, including its file/folder tree."""
    dataset = get_dataset_by_id(db, tenant.user, dataset_id, organization_id=tenant.organization_id)
    dataset.tree = get_dataset_tree_for_dataset(db, tenant.user, dataset.id, organization_id=tenant.organization_id)
    return dataset


@file_router.patch("/datasets/{dataset_id}", response_model=DatasetResponse)
def modify_dataset(
    dataset_id: str,
    payload: DatasetUpdate,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Modify metadata of an existing dataset within active organization.

    Args:
        dataset_id (str): The unique dataset ID.
        payload (DatasetUpdate): The patch updates schema.
        tenant (TenantContext): Resolved tenant and user context.
        db (Session): The active database session.

    Returns:
        DatasetResponse: The updated dataset details.
    """
    return update_dataset(db, tenant.user, dataset_id, payload, organization_id=tenant.organization_id)


@file_router.delete("/datasets/{dataset_id}", status_code=status.HTTP_200_OK)
def remove_dataset(
    dataset_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Soft-delete a dataset if no files are currently attached.

    Args:
        dataset_id (str): The unique dataset ID.
        tenant (TenantContext): Resolved tenant and user context.
        db (Session): The active database session.

    Returns:
        dict: Confirmation payload of the deletion.
    """
    delete_dataset(db, tenant.user, dataset_id, organization_id=tenant.organization_id)
    return {"status": "deleted", "dataset_id": dataset_id}


@file_router.post("/datasets/{dataset_id}/files", response_model=DatasetAttachFileResponse)
def attach_file(
    dataset_id: str,
    payload: DatasetAttachFileRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Attach an existing uploaded file to a dataset within active organization (zero-I/O).

    Args:
        dataset_id (str): Target dataset ID.
        payload (DatasetAttachFileRequest): File attachment details.
        tenant (TenantContext): Resolved tenant and user context.
        db (Session): The active database session.

    Returns:
        DatasetAttachFileResponse: The attach result schema.
    """
    return attach_file_to_dataset(db, tenant.user, dataset_id, payload, organization_id=tenant.organization_id)


@file_router.get("/users/me/integrations", response_model=UserIntegrationsResponse)
def get_user_integrations_endpoint(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Return the authenticated user's cloud integrations connection status.
    """
    return get_user_integrations(tenant.user)


@file_router.get("/users/me/google-token")
def get_user_google_token_endpoint(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Obtain a fresh Google OAuth access token for Google Picker API initialization.
    """
    token = get_user_google_access_token(tenant.user)
    return {"access_token": token}


@file_router.get("/ingest/status/{job_id}", response_model=IngestionJobStatusResponse)
def get_ingestion_job_status_endpoint(
    job_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Poll the status and progress of an asynchronous cloud ingestion job.
    """
    return get_ingestion_job_status(db, tenant.user, job_id)


@file_router.post("/ingest/gdrive/init", response_model=GDriveIngestResponse)
def ingest_gdrive_init_endpoint(
    payload: GDriveIngestInitRequest,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a Google Drive file by file_id.
    """
    return initiate_gdrive_ingestion(
        db=db,
        user=tenant.user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.file_id,
        background_tasks=background_tasks,
        folder_id=payload.folder_id,
        filename=payload.filename,
        mime_type=payload.mime_type,
        organization_id=tenant.organization_id,
    )


@file_router.post("/ingest/gdrive/url", response_model=GDriveIngestResponse)
def ingest_gdrive_url_endpoint(
    payload: GDriveIngestUrlRequest,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a Google Drive file by shared URL.
    """
    return initiate_gdrive_ingestion(
        db=db,
        user=tenant.user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.gdrive_url,
        background_tasks=background_tasks,
        folder_id=payload.folder_id,
        filename=payload.filename,
        mime_type=payload.mime_type,
        organization_id=tenant.organization_id,
    )


@file_router.get("/ingest/sharepoint/tree", response_model=SharepointTreeResponse)
def get_sharepoint_tree_endpoint(
    folder_id: str | None = None,
    drive_id: str | None = None,
    site_id: str | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Explore Microsoft SharePoint / Graph API directory tree and items.
    """
    return get_sharepoint_tree(
        db=db,
        user=tenant.user,
        folder_id=folder_id,
        drive_id=drive_id,
        site_id=site_id,
    )


@file_router.post("/ingest/sharepoint/init", response_model=SharepointIngestResponse)
def ingest_sharepoint_init_endpoint(
    payload: SharepointIngestInitRequest,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a SharePoint file by item ID.
    """
    return initiate_sharepoint_ingestion(
        db=db,
        user=tenant.user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.file_id,
        background_tasks=background_tasks,
        quick_xor_hash=payload.quick_xor_hash,
        folder_id=payload.folder_id,
        filename=payload.filename,
        organization_id=tenant.organization_id,
    )


@file_router.post("/ingest/sharepoint/url", response_model=SharepointIngestResponse)
def ingest_sharepoint_url_endpoint(
    payload: SharepointIngestUrlRequest,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    Initiate asynchronous ingestion of a SharePoint file by shared URL.
    """
    return initiate_sharepoint_ingestion(
        db=db,
        user=tenant.user,
        dataset_id=payload.dataset_id,
        file_id_or_url=payload.sharepoint_url,
        background_tasks=background_tasks,
        quick_xor_hash=payload.quick_xor_hash,
        folder_id=payload.folder_id,
        filename=payload.filename,
        organization_id=tenant.organization_id,
    )

