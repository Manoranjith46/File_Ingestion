"""SQLAlchemy models for file ingestion and tracking."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SQLEnum, ForeignKey, String, Text, func
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
    files = relationship("IngestedFile", back_populates="dataset", cascade="all, delete-orphan")

    @property
    def file_count(self) -> int:
        """Return the number of files linked to this dataset."""
        return len(self.files)

    @property
    def dataset_name(self) -> str:
        """Return the canonical dataset name under the response field expected by clients."""
        return self.name

    @property
    def source_type(self) -> str | None:
        """Return a single source type for this dataset when it can be inferred from linked files."""
        for file in self.files:
            if file.provider is not None:
                return file.provider.value if isinstance(file.provider, Enum) else str(file.provider)
        return None


class IngestedFileProviderType(str, Enum):
    FTP = "FTP"
    Local = "Local"
    GDrive = "GDrive"
    Sharepoint = "Sharepoint"


class IngestedFile(Base):
    """Represent a flattened ingested file tracking record."""

    __tablename__ = "ingested_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), nullable=True, index=True)
    provider: Mapped[IngestedFileProviderType] = mapped_column(
        SQLEnum(IngestedFileProviderType, name="ingested_file_provider_type"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    physical_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    master_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User")
    dataset = relationship("Dataset", back_populates="files")
