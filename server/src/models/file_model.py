"""SQLAlchemy models for virtual directory upload management."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
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
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User")
    mappings = relationship("DatasetFolderFilesMapping", back_populates="dataset", cascade="all, delete-orphan")


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


class UploadedFile(Base):
    """Represent a physical file details uploaded to the server."""

    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    master_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    physical_path: Mapped[str] = mapped_column(String(500), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)

    mappings = relationship("DatasetFolderFilesMapping", back_populates="file", cascade="all, delete-orphan")


class DatasetFolderFilesMapping(Base):
    """Represent the mapping between a dataset, virtual folder, and uploaded file."""

    __tablename__ = "dataset_folder_files_mapping"
    __table_args__ = (
        UniqueConstraint("dataset_id", "folder_id", "file_id", name="uq_dataset_folder_file"),
    )

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
