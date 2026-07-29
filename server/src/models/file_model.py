"""SQLAlchemy models for virtual directory upload management."""

from __future__ import annotations

import uuid
from datetime import datetime

from enum import Enum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SQLEnum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.auth_model import Base


def generate_uuid() -> str:
    """
    Generate a stable UUID string for primary keys.

    Returns:
        str: The generated UUID string.
    """
    return str(uuid.uuid4())


class Dataset(Base):
    """Represent a dataset catalog entry owned by a user."""

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="Created", nullable=False)
    language: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    mappings = relationship("DatasetFolderFilesMapping", back_populates="dataset", cascade="all, delete-orphan")

    @property
    def file_count(self) -> int:
        """Return the number of files linked to this dataset."""
        return len(self.mappings)


class Folder(Base):
    """Represent a folder created by a user in the virtual upload tree."""

    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("user_id", "parent_id", "name", name="uq_user_parent_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)

    parent = relationship("Folder", remote_side=[id], back_populates="children")
    children = relationship("Folder", back_populates="parent", cascade="all, delete-orphan")
    mappings = relationship("DatasetFolderFilesMapping", back_populates="folder", cascade="all, delete-orphan")


class UploadedFileSourceType(str, Enum):
    FTP = "FTP"
    GDrive = "GDrive"
    Sharepoint = "Sharepoint"


class UploadedFile(Base):
    """Represent a physical file details uploaded to the server."""

    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    master_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    physical_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_type: Mapped[UploadedFileSourceType | None] = mapped_column(SQLEnum(UploadedFileSourceType, name="uploaded_file_source_type"), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)

    mappings = relationship("DatasetFolderFilesMapping", back_populates="file", cascade="all, delete-orphan")


class DatasetFolderFilesMapping(Base):
    """Represent the mapping between a dataset, virtual folder, and uploaded file."""

    __tablename__ = "dataset_folder_files_mapping"
    __table_args__ = (
        UniqueConstraint("dataset_id", "folder_id", "file_id", name="uq_dataset_folder_file"),
    )
    __mapper_args__ = {"confirm_deleted_rows": False}

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)

    dataset = relationship("Dataset", back_populates="mappings")
    folder = relationship("Folder", back_populates="mappings")
    file = relationship("UploadedFile", back_populates="mappings")
    user = relationship("User")


class ProviderHashMapping(Base):
    """
    Rosetta Stone translation table mapping cloud provider hashes to canonical master_hash.
    """

    __tablename__ = "provider_hash_mappings"
    __table_args__ = (
        UniqueConstraint("provider_name", "provider_hash", name="uq_provider_name_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    provider_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    provider_file_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    master_hash: Mapped[str] = mapped_column(ForeignKey("uploaded_files.master_hash", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)

    uploaded_file = relationship("UploadedFile", foreign_keys=[master_hash], primaryjoin="ProviderHashMapping.master_hash == UploadedFile.master_hash")


class AsyncIngestionJob(Base):
    """
    Ledger tracking background ingestion tasks for cloud provider downloads.
    """

    __tablename__ = "async_ingestion_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url_or_id: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)
    progress_percentage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    master_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_id: Mapped[str | None] = mapped_column(ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    dataset = relationship("Dataset")
    folder = relationship("Folder")
    file = relationship("UploadedFile")

