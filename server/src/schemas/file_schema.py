"""Pydantic schemas for file ingestion endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


CHUNK_SIZE_BYTES = 5242880
UploadStatus = Literal[
    "Created",
    "In Progress",
    "Completed",
    "created",
    "in progress",
    "completed",
    "duplicate_short_circuit",
    "duplicate_suspected",
    "success",
    "deleted",
    "attached",
]
DatasetLanguage = str
DatasetStatus = str


class UploadInitRequest(BaseModel):
    """Schema for upload initialization requests."""

    dataset_id: str = Field(min_length=1)
    filename: str = Field(min_length=1, max_length=255)
    filesize: int = Field(gt=0)
    master_hash: str = Field(min_length=64, max_length=64)
    relative_path: str | None = Field(default=None, min_length=0, max_length=500)
    source_type: Literal["FTP", "GDrive", "Sharepoint"] | None = Field(default=None)


class UploadInitResponse(BaseModel):
    """Schema for upload initialization responses."""

    upload_id: str
    chunk_size: int = CHUNK_SIZE_BYTES
    total_chunks: int
    status: UploadStatus


class UploadChunkRequest(BaseModel):
    """Schema for chunk upload requests."""

    upload_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    chunk_hash: str = Field(min_length=64, max_length=64)


class UploadChunkResponse(BaseModel):
    """Schema for chunk upload responses."""

    status: UploadStatus
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

    status: UploadStatus
    file_id: str
    folder_id: str | None = None


class UploadDeleteRequest(BaseModel):
    """Schema for upload delete requests."""

    upload_id: str = Field(min_length=1)


class UploadDeleteResponse(BaseModel):
    """Schema for upload delete responses."""

    status: UploadStatus
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
    language: DatasetLanguage


class DatasetUpdate(BaseModel):
    """Schema for dataset update requests."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: DatasetStatus | None = Field(default=None)
    target_dataset_id: str | None = Field(default=None, min_length=1, max_length=255)
    file_id: str | None = Field(default=None, min_length=1, max_length=255)
    folder_id: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)
    language: DatasetLanguage | None = Field(default=None)


class DatasetResponse(BaseModel):
    """Schema for dataset details responses."""

    id: str
    user_id: str
    name: str
    description: str | None = None
    status: DatasetStatus = "Created"
    language: DatasetLanguage | None = None
    created_at: datetime
    updated_at: datetime
    file_count: int
    tree: UploadsTreeResponse | None = None

    model_config = ConfigDict(from_attributes=True)


class DatasetAttachFileRequest(BaseModel):
    """Schema for attaching an existing file to a dataset."""

    file_id: str = Field(min_length=1)
    relative_path: str | None = Field(default=None, max_length=500)


class DatasetAttachFileResponse(BaseModel):
    """Schema for file attach responses."""

    status: UploadStatus
    dataset_id: str
    file_id: str
    folder_id: str | None = None


class UserIntegrationsResponse(BaseModel):
    """Schema for user cloud integration connection status."""

    google_connected: bool = False
    microsoft_connected: bool = False


class IngestionJobStatusResponse(BaseModel):
    """Schema for async cloud ingestion job status polling."""

    job_id: str
    provider: str
    filename: str
    status: str
    progress_percentage: int = 0
    error_message: str | None = None
    file_id: str | None = None
    master_hash: str | None = None

    model_config = ConfigDict(from_attributes=True)


class GDriveIngestInitRequest(BaseModel):
    """Schema for initiating Google Drive file ingestion by file_id."""

    dataset_id: str = Field(min_length=1)
    file_id: str = Field(min_length=1)
    folder_id: str | None = Field(default=None)
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=255)


class GDriveIngestUrlRequest(BaseModel):
    """Schema for initiating Google Drive file ingestion by shared URL."""

    dataset_id: str = Field(min_length=1)
    gdrive_url: str = Field(min_length=1, max_length=1000)
    folder_id: str | None = Field(default=None)
    filename: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=255)


class GDriveIngestResponse(BaseModel):
    """Schema for Google Drive ingestion initiation response."""

    job_id: str
    status: str = "pending"
    message: str = "Google Drive ingestion job initiated"


class SharepointItem(BaseModel):
    """Schema for a single Microsoft SharePoint / Graph API file or folder item."""

    id: str
    name: str
    is_folder: bool
    mime_type: str | None = None
    size_bytes: int = 0
    quick_xor_hash: str | None = None
    web_url: str | None = None
    parent_id: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SharepointTreeResponse(BaseModel):
    """Schema for returning SharePoint folder hierarchy listings."""

    items: list[SharepointItem] = Field(default_factory=list)
    drive_id: str | None = None
    parent_folder_id: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SharepointIngestInitRequest(BaseModel):
    """Schema for initiating SharePoint file ingestion by item ID."""

    dataset_id: str = Field(min_length=1)
    file_id: str = Field(min_length=1)
    quick_xor_hash: str | None = Field(default=None)
    folder_id: str | None = Field(default=None)
    filename: str | None = Field(default=None, max_length=255)


class SharepointIngestUrlRequest(BaseModel):
    """Schema for initiating SharePoint file ingestion by shared URL."""

    dataset_id: str = Field(min_length=1)
    sharepoint_url: str = Field(min_length=1, max_length=1000)
    quick_xor_hash: str | None = Field(default=None)
    folder_id: str | None = Field(default=None)
    filename: str | None = Field(default=None, max_length=255)


class SharepointIngestResponse(BaseModel):
    """Schema for SharePoint ingestion initiation response."""

    job_id: str
    status: str = "pending"
    message: str = "SharePoint ingestion job initiated"
    is_instant_deduplicated: bool = False





