"""HTTP routes for External FTP Pull Ingestion.

Endpoints
    ``POST /ingest/ftp/connect``      — Validate and store FTP credentials.
    ``POST /ingest/ftp/disconnect``    — Graceful drain or force-nuke disconnect.

These routes are mounted under the ``/v1`` prefix by ``main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from config.database import get_db
from models.auth_model import User
from schemas.ftp_schema import (
    FtpBatchStatusResponse,
    FtpConnectRequest,
    FtpConnectResponse,
    FtpDisconnectRequest,
    FtpDisconnectResponse,
    FtpInitRequest,
    FtpInitResponse,
    FtpResumeRequest,
    FtpResumeResponse,
    FtpTreeResponse,
)
from services.auth_services import get_current_user
from services.ext_ftp_service import (
    connect_external_ftp,
    disconnect_external_ftp,
    get_batch_ingestion_status,
    get_external_ftp_tree,
    initiate_external_ftp_ingestion,
    resume_failed_ftp_jobs,
)

ftp_router = APIRouter()


# ---------------------------------------------------------------------------
# Helper — mirrors _resolve_current_user in file_routes.py
# ---------------------------------------------------------------------------


def _resolve_current_user(authorization: str | None, db: Session) -> User:
    """Resolve the current user from a bearer token.

    Args:
        authorization (str | None): The ``Authorization`` header value.
        db (Session): An active SQLAlchemy session.

    Returns:
        User: The authenticated user ORM instance.

    Raises:
        HTTPException: If the token is missing or invalid.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing access token",
        )
    token = authorization.removeprefix("Bearer ").strip()
    return get_current_user(db, token)


# ---------------------------------------------------------------------------
# POST /ingest/ftp/connect
# ---------------------------------------------------------------------------


@ftp_router.post(
    "/ingest/ftp/connect",
    response_model=FtpConnectResponse,
    status_code=status.HTTP_200_OK,
    summary="Connect to an external FTP/FTPS server",
    description=(
        "Validates credentials against the remote FTP server, encrypts "
        "the password using Fernet (AES-256), and stores the session "
        "in memory-only Redis with a configurable idle TTL."
    ),
)
def ftp_connect(
    body: FtpConnectRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FtpConnectResponse:
    """Handle ``POST /v1/ingest/ftp/connect``.

    Args:
        body (FtpConnectRequest): The connection parameters.
        authorization (str | None): Bearer access token from the header.
        db (Session): Injected database session.

    Returns:
        FtpConnectResponse: Session metadata on success.
    """
    user = _resolve_current_user(authorization, db)
    result = connect_external_ftp(
        user=user,
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        duration_seconds=body.duration_seconds,
        until_i_stop=body.until_i_stop,
        graceful_expiry=body.graceful_expiry,
        allow_insecure=body.allow_insecure,
    )
    return FtpConnectResponse(**result)


# ---------------------------------------------------------------------------
# POST /ingest/ftp/disconnect
# ---------------------------------------------------------------------------


@ftp_router.post(
    "/ingest/ftp/disconnect",
    response_model=FtpDisconnectResponse,
    status_code=status.HTTP_200_OK,
    summary="Disconnect from the external FTP session",
    description=(
        "Supports graceful draining (default) or force cancellation "
        "with reference-counted cleanup of physical files."
    ),
)
def ftp_disconnect(
    body: FtpDisconnectRequest | None = None,
    force: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FtpDisconnectResponse:
    """Handle ``POST /v1/ingest/ftp/disconnect``.

    Args:
        body (FtpDisconnectRequest | None): Optional body for force disconnect.
        force (bool): Query parameter — ``True`` to force-cancel all transfers.
        authorization (str | None): Bearer access token from the header.
        db (Session): Injected database session.

    Returns:
        FtpDisconnectResponse: Disconnect result payload.
    """
    user = _resolve_current_user(authorization, db)
    delete_completed_files = body.delete_completed_files if body else False
    result = disconnect_external_ftp(
        user=user,
        db=db,
        force=force,
        delete_completed_files=delete_completed_files,
    )
    return FtpDisconnectResponse(**result)


# ---------------------------------------------------------------------------
# GET /ingest/ftp/tree
# ---------------------------------------------------------------------------


@ftp_router.get(
    "/ingest/ftp/tree",
    response_model=FtpTreeResponse,
    status_code=status.HTTP_200_OK,
    summary="Lazily fetch remote FTP directory tree",
    description=(
        "Lazily fetches the directory contents for a specific remote FTP path "
        "without retrieving full server trees, preventing HTTP timeouts."
    ),
)
def ftp_tree(
    path: str = Query(default="/"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FtpTreeResponse:
    """Handle ``GET /v1/ingest/ftp/tree``.

    Args:
        path (str): Target remote directory path (default ``"/"``).
        authorization (str | None): Bearer access token from header.
        db (Session): Injected database session.

    Returns:
        FtpTreeResponse: Standardized directory tree items.
    """
    user = _resolve_current_user(authorization, db)
    result = get_external_ftp_tree(user_id=user.id, path=path)
    return FtpTreeResponse(**result)


# ---------------------------------------------------------------------------
# POST /ingest/ftp/init
# ---------------------------------------------------------------------------


@ftp_router.post(
    "/ingest/ftp/init",
    response_model=FtpInitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue remote FTP files or folders for background ingestion",
    description=(
        "Queues selected remote FTP files or folders for background ingestion. "
        "Rejects requests with 409 Conflict if the session is currently in a draining state "
        "or if a filename collision occurs and auto_rename is false."
    ),
)
def ftp_init(
    body: FtpInitRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FtpInitResponse:
    """Handle ``POST /v1/ingest/ftp/init``.

    Args:
        body (FtpInitRequest): Queue parameters.
        authorization (str | None): Bearer access token from header.
        db (Session): Injected database session.

    Returns:
        FtpInitResponse: List of queued background jobs (202 Accepted).
    """
    user = _resolve_current_user(authorization, db)
    result = initiate_external_ftp_ingestion(
        user=user,
        db=db,
        dataset_id=body.dataset_id,
        target_folder_id=body.target_folder_id,
        items=[item.model_dump() for item in body.items],
        auto_rename=body.auto_rename,
    )
    return FtpInitResponse(**result)


# ---------------------------------------------------------------------------
# GET /ingest/status (Batch Polling)
# ---------------------------------------------------------------------------


@ftp_router.get(
    "/ingest/status",
    response_model=FtpBatchStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Batch status polling endpoint for UI progress bars",
    description=(
        "Fetches batch progress updates for active and historical ingestion jobs, "
        "merging PostgreSQL static data with live Redis progress bytes."
    ),
)
def batch_ingestion_status(
    status: str | None = Query(default=None, description="Comma-separated statuses to filter, e.g. in_progress,pending,failed"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FtpBatchStatusResponse:
    """Handle ``GET /v1/ingest/status``.

    Args:
        status (str | None): Optional status filter.
        authorization (str | None): Bearer access token from header.
        db (Session): Injected database session.

    Returns:
        FtpBatchStatusResponse: Batch jobs progress snapshot.
    """
    user = _resolve_current_user(authorization, db)
    result = get_batch_ingestion_status(user=user, db=db, status_filter=status)
    return FtpBatchStatusResponse(**result)


# ---------------------------------------------------------------------------
# POST /ingest/ftp/resume (Medic Recovery)
# ---------------------------------------------------------------------------


@ftp_router.post(
    "/ingest/ftp/resume",
    response_model=FtpResumeResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume failed FTP ingestion jobs using checkpoint manifests",
    description=(
        "Resumes failed ingestion jobs using disk checkpoint manifests. "
        "Rejects with 410 Gone if the FTP session credentials have expired."
    ),
)
def ftp_resume(
    body: FtpResumeRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FtpResumeResponse:
    """Handle ``POST /v1/ingest/ftp/resume``.

    Args:
        body (FtpResumeRequest): List of failed job UUIDs to resume.
        authorization (str | None): Bearer access token from header.
        db (Session): Injected database session.

    Returns:
        FtpResumeResponse: Confirmation payload (202 Accepted).
    """
    user = _resolve_current_user(authorization, db)
    result = resume_failed_ftp_jobs(user=user, db=db, job_ids=body.job_ids)
    return FtpResumeResponse(**result)


