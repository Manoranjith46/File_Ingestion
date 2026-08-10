"""Pydantic V2 schemas for External FTP Pull Ingestion endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FtpConnectRequest(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/connect`` request body.

    Attributes:
        host: The external FTP server hostname or IP.
        port: The FTP control port (default 21).
        username: The FTP login username.
        password: The FTP login password (encrypted before storage).
        duration_seconds: Session lifetime in seconds (default 7200 / 2h).
        until_i_stop: If ``True``, the session stays alive until manual disconnect.
        graceful_expiry: If ``True``, expire gracefully (drain) rather than hard-cut.
        allow_insecure: If ``True``, permit plaintext FTP when TLS is rejected.
    """

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=21, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)
    duration_seconds: int = Field(default=7200, ge=60, le=86400)
    until_i_stop: bool = Field(default=False)
    graceful_expiry: bool = Field(default=True)
    allow_insecure: bool = Field(default=False)


class FtpSessionInfo(BaseModel):
    """Metadata snapshot of an active external FTP session.

    Attributes:
        host: The connected FTP server hostname.
        username: The FTP login username.
        protocol: ``"ftps"`` or ``"ftp"`` depending on TLS negotiation result.
        state: Current session lifecycle state (``active``, ``draining``, ``disconnected``).
        idle_ttl_seconds: Seconds of inactivity before Redis auto-expires the session.
        target_expiry_time: Unix timestamp when the session is scheduled to expire.
    """

    host: str
    username: str
    protocol: str
    state: str
    idle_ttl_seconds: int
    target_expiry_time: int

    model_config = ConfigDict(from_attributes=True)


class FtpConnectResponse(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/connect`` success response.

    Attributes:
        status: ``"success"`` on successful connection.
        message: Human-readable result description.
        session: The session metadata snapshot.
    """

    status: str
    message: str
    session: FtpSessionInfo


class FtpDisconnectRequest(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/disconnect`` request body.

    Only required when ``force=true`` is passed as a query parameter.

    Attributes:
        delete_completed_files: If ``True`` during a force disconnect,
            run ref-count cleanup on completed files from this session.
    """

    delete_completed_files: bool = Field(default=False)


class FtpDisconnectResponse(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/disconnect`` response.

    Attributes:
        status: ``"success"`` or ``"conflict"`` depending on the disconnect path.
        message: Human-readable result description.
        cancelled_jobs: Number of jobs cancelled (force disconnect only).
        deleted_orphan_files: Number of orphaned physical files deleted.
        active_jobs: Number of in-flight jobs (draining path only).
        draining: ``True`` when the session entered draining state.
    """

    status: str
    message: str
    cancelled_jobs: int | None = None
    deleted_orphan_files: int | None = None
    active_jobs: int | None = None
    draining: bool | None = None


class FtpTreeItem(BaseModel):
    """Schema for a single file or directory item in the FTP tree.

    Attributes:
        name: Filename or directory name.
        path: Absolute remote path of the item.
        is_folder: ``True`` if directory, ``False`` if file.
        size_bytes: Size in bytes (0 for folders).
        modified_at: ISO 8601 UTC timestamp or ``None`` if unavailable.
    """

    name: str
    path: str
    is_folder: bool
    size_bytes: int = 0
    modified_at: str | None = None

    model_config = ConfigDict(from_attributes=True)


class FtpTreeResponse(BaseModel):
    """Schema for the ``GET /v1/ingest/ftp/tree`` response payload.

    Attributes:
        current_path: Normalized current directory path.
        parent_path: Parent directory path (or ``"/"`` if at root).
        items: List of files and folders inside ``current_path``.
    """

    current_path: str
    parent_path: str
    items: list[FtpTreeItem] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class FtpInitItem(BaseModel):
    """Schema for a single remote FTP item submitted for background ingestion.

    Attributes:
        path: Absolute remote FTP path of the file or directory.
        is_folder: ``True`` if target is a folder to be recursively ingested.
        size_bytes: Size in bytes (0 for folders).
    """

    path: str = Field(min_length=1)
    is_folder: bool = Field(default=False)
    size_bytes: int = Field(default=0, ge=0)


class FtpInitRequest(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/init`` request body.

    Attributes:
        dataset_id: Target dataset UUID.
        target_folder_id: Target dataset folder UUID (optional).
        auto_rename: If True, append (1) on filename collision instead of 409 Conflict.
        items: List of remote FTP file/folder items to queue.
    """

    dataset_id: str = Field(min_length=1)
    target_folder_id: str | None = Field(default=None)
    auto_rename: bool = Field(default=False)
    items: list[FtpInitItem] = Field(min_length=1)


class FtpInitJobItem(BaseModel):
    """Metadata snapshot for a queued background ingestion job.

    Attributes:
        job_id: The created ``AsyncIngestionJob`` UUID.
        path: Remote FTP path being downloaded.
        status: Initial status (default ``"pending"``).
        filename: Base filename.
        filesize: Size in bytes.
        source: Provider label (``"FTP"``).
        dataset_id: Target dataset UUID.
        dataset_name: Name of target dataset.
    """

    job_id: str
    path: str
    status: str = "pending"
    filename: str
    filesize: int
    source: str = "FTP"
    dataset_id: str
    dataset_name: str

    model_config = ConfigDict(from_attributes=True)


class FtpInitResponse(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/init`` success response (202 Accepted).

    Attributes:
        status: Job status indicator (``"processing"``).
        message: Human-readable queue summary.
        jobs: List of created job snapshots.
    """

    status: str = "processing"
    message: str
    jobs: list[FtpInitJobItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# GET /v1/ingest/status (Batch Polling)
# ---------------------------------------------------------------------------


class FtpBatchStatusJobItem(BaseModel):
    """Schema for a single job item in the batch polling status response.

    Attributes:
        job_id: The ingestion job UUID.
        provider: Origin provider label (e.g. ``"FTP"``).
        filename: Target filename.
        dataset_name: Target dataset name.
        filesize: Formatted or byte file size.
        status: Current status (``pending``, ``in_progress``, ``completed``, ``failed``).
        progress_percentage: Percentage calculated from bytes downloaded / total bytes.
        bytes_downloaded: Downloaded bytes count.
        total_bytes: Total file bytes count.
        error_message: Error string if job failed.
    """

    job_id: str
    provider: str
    filename: str
    dataset_name: str | None = None
    filesize: str | None = None
    status: str
    progress_percentage: int = 0
    bytes_downloaded: int = 0
    total_bytes: int = 0
    error_message: str | None = None

    model_config = ConfigDict(from_attributes=True)


class FtpBatchStatusResponse(BaseModel):
    """Schema for the ``GET /v1/ingest/status`` batch response.

    Attributes:
        total_active_jobs: Count of matching jobs returned.
        jobs: Array of job status snapshots.
    """

    total_active_jobs: int = 0
    jobs: list[FtpBatchStatusJobItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /v1/ingest/ftp/resume (Medic Recovery)
# ---------------------------------------------------------------------------


class FtpResumeRequest(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/resume`` request body.

    Attributes:
        job_ids: List of failed job UUIDs to resume.
    """

    job_ids: list[str] = Field(min_length=1)


class FtpResumeResponse(BaseModel):
    """Schema for the ``POST /v1/ingest/ftp/resume`` success response (202 Accepted).

    Attributes:
        status: Status indicator (``"processing"``).
        message: Human-readable message.
        resumed_jobs: List of resumed job UUIDs.
    """

    status: str = "processing"
    message: str
    resumed_jobs: list[str] = Field(default_factory=list)



