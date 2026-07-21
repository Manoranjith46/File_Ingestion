"""SQLAlchemy models for deduplicated file ingestion."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, BigInteger, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.auth_model import Base


class PhysicalFile(Base):
    """Represent a deduplicated physical file stored on disk."""

    __tablename__ = "physical_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    master_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)

    uploads = relationship("UserUploadMapping", back_populates="physical_file")


class UserUploadMapping(Base):
    """Map a user upload to a deduplicated physical file."""

    __tablename__ = "user_upload_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    physical_file_id: Mapped[int] = mapped_column(ForeignKey("physical_files.id"), nullable=False)
    client_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)

    physical_file = relationship("PhysicalFile", back_populates="uploads")