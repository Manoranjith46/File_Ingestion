"""add file ingestion tables

Revision ID: c8f2d6a4b9e1
Revises: f09f8c95b82f
Create Date: 2026-07-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c8f2d6a4b9e1"
down_revision: Union[str, Sequence[str], None] = "f09f8c95b82f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the file ingestion tables."""
    op.create_table(
        "physical_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("master_hash", sa.String(length=64), nullable=False),
        sa.Column("file_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("master_hash"),
        sa.UniqueConstraint("file_fingerprint"),
    )
    op.create_table(
        "user_upload_mappings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("physical_file_id", sa.Integer(), nullable=False),
        sa.Column("client_filename", sa.String(length=255), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["physical_file_id"], ["physical_files.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop the file ingestion tables."""
    op.drop_table("user_upload_mappings")
    op.drop_table("physical_files")
