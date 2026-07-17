"""Pydantic schemas for file ingestion requests and responses."""

from pydantic import BaseModel, Field


CHUNK_SIZE_BYTES = 5 * 1024 * 1024


class UploadInitRequest(BaseModel):
    """Validate file upload initialization requests."""

    filename: str = Field(min_length=1, max_length=255)
    filesize: int = Field(gt=0)


class UploadInitResponse(BaseModel):
    """Return upload session metadata for chunked uploads."""

    upload_id: str
    filename: str
    filesize: int
    chunk_size: int = CHUNK_SIZE_BYTES
    total_chunks: int


class UploadChunkRequest(BaseModel):
    """Validate chunk metadata submitted with each upload part."""

    upload_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    chunk_hash: str = Field(min_length=64, max_length=64)


class UploadFinalizeRequest(BaseModel):
    """Validate finalize requests for completed uploads."""

    upload_id: str = Field(min_length=1)


class UploadChunkResponse(BaseModel):
    """Return chunk processing state to the client."""

    status: str
    upload_id: str
    chunk_index: int
    bytes_received: int
    received_chunks: int
    total_chunks: int
    chunk_size: int = CHUNK_SIZE_BYTES
    complete: bool


class UploadListItem(BaseModel):
    """Represent a successfully uploaded file in a list response."""

    filename: str
    filesize: int


class UploadListResponse(BaseModel):
    """Return paginated uploaded-file metadata."""

    page: int
    limit: int
    total_files: int
    files: list[UploadListItem]


class UploadFinalizeResponse(BaseModel):
    """Return finalize metadata after a completed upload is merged."""

    status: str
    upload_id: str
    physical_file_id: int
    file_path: str
    master_hash: str
    complete: bool = True