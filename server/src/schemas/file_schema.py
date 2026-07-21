"""Pydantic schemas for file ingestion endpoints."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


CHUNK_SIZE_BYTES = 5242880


class UploadInitRequest(BaseModel):
    """Schema for upload initialization requests."""

    dataset_id: str = Field(min_length=1)
    filename: str = Field(min_length=1, max_length=255)
    filesize: int = Field(gt=0)
    master_hash: str = Field(min_length=64, max_length=64)
    relative_path: str | None = Field(default=None, min_length=0, max_length=500)


class UploadInitResponse(BaseModel):
    """Schema for upload initialization responses."""

    upload_id: str
    chunk_size: int = CHUNK_SIZE_BYTES
    total_chunks: int
    status: str


class UploadChunkRequest(BaseModel):
    """Schema for chunk upload requests."""

    upload_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    chunk_hash: str = Field(min_length=64, max_length=64)


class UploadChunkResponse(BaseModel):
    """Schema for chunk upload responses."""

    status: str
    upload_id: str
    chunk_index: int
    bytes_received: int
    received_chunks: int
    total_chunks: int
    chunk_size: int = CHUNK_SIZE_BYTES
    complete: bool


class UploadFinalizeRequest(BaseModel):
    """Schema for upload finalization requests."""

    upload_id: str = Field(min_length=1)
    master_hash: str = Field(min_length=64, max_length=64)


class UploadFinalizeResponse(BaseModel):
    """Schema for upload finalization responses."""

    status: str
    file_id: str
    folder_id: str | None = None


class UploadDeleteRequest(BaseModel):
    """Schema for upload delete requests."""

    upload_id: str = Field(min_length=1)


class UploadDeleteResponse(BaseModel):
    """Schema for upload delete responses."""

    status: str
    file_id: str


class UploadTreeNode(BaseModel):
    """Schema for nested uploaded file tree responses."""

    id: str
    type: str
    name: str
    size: int | None = None
    children: list["UploadTreeNode"] | None = None


class UploadsTreeResponse(UploadTreeNode):
    """Schema for the uploads tree response root node."""

    pass


class DatasetCreate(BaseModel):
    """Schema for dataset creation requests."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)


class DatasetUpdate(BaseModel):
    """Schema for dataset update requests."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)


class DatasetResponse(BaseModel):
    """Schema for dataset details responses."""

    id: str
    user_id: str
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DatasetAttachFileRequest(BaseModel):
    """Schema for attaching an existing file to a dataset."""

    file_id: str = Field(min_length=1)
    relative_path: str | None = Field(default=None, max_length=500)


class DatasetAttachFileResponse(BaseModel):
    """Schema for file attach responses."""

    status: str
    dataset_id: str
    file_id: str
    folder_id: str | None = None

