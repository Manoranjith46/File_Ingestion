"""HTTP routes for file ingestion workflows."""

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from config.database import get_db
from models.auth_model import User
from schemas.file_schema import (
    UploadChunkRequest,
    UploadChunkResponse,
    UploadFinalizeResponse,
    UploadFinalizeRequest,
    UploadInitRequest,
    UploadInitResponse,
    UploadListResponse,
)
from services.auth_services import get_current_user
from services.file_services import finalize_upload, initialize_upload, list_user_uploads, process_upload_chunk


file_router = APIRouter(prefix="/v1")


def _current_user(authorization: str | None, db: Session) -> User:
    """Resolve the current user from a bearer token."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing access token")
    access_token = authorization.removeprefix("Bearer ").strip()
    return get_current_user(db, access_token)


@file_router.post("/upload/init", response_model=UploadInitResponse)
def upload_init(
    payload: UploadInitRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Initialize a chunked file upload session."""
    user = _current_user(authorization, db)
    return initialize_upload(db, user, payload)


@file_router.post("/upload/chunk", response_model=UploadChunkResponse)
def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(..., ge=0),
    chunk_hash: str = Form(..., min_length=64, max_length=64),
    range_header: str | None = Header(default=None, alias="Range"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    chunk_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Process a single uploaded chunk."""
    if range_header is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Range header is required")
    user = _current_user(authorization, db)
    payload = UploadChunkRequest(upload_id=upload_id, chunk_index=chunk_index, chunk_hash=chunk_hash)
    chunk_bytes = chunk_file.file.read()
    return process_upload_chunk(db, user, payload, chunk_bytes)


@file_router.post("/upload/finalize", response_model=UploadFinalizeResponse)
def upload_finalize(
    payload: UploadFinalizeRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Finalize an upload after all chunks have been received."""
    user = _current_user(authorization, db)
    return finalize_upload(db, user, payload.upload_id)


@file_router.get("/uploads", response_model=UploadListResponse)
def uploads(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1),
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's uploaded files."""
    user = _current_user(authorization, db)
    return list_user_uploads(db, user, page=page, limit=limit)