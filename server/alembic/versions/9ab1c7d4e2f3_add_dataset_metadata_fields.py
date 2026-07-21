"""add dataset metadata fields

Revision ID: 9ab1c7d4e2f3
Revises: 80422981c893
Create Date: 2026-07-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9ab1c7d4e2f3"
down_revision: Union[str, Sequence[str], None] = "80422981c893"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add dataset metadata columns."""
    op.add_column("datasets", sa.Column("source_type", sa.String(length=255), nullable=True))
    op.add_column("datasets", sa.Column("content_type", sa.String(length=255), nullable=True))
    op.add_column("datasets", sa.Column("format", sa.String(length=255), nullable=True))
    op.add_column("datasets", sa.Column("language", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Remove dataset metadata columns."""
    op.drop_column("datasets", "language")
    op.drop_column("datasets", "format")
    op.drop_column("datasets", "content_type")
    op.drop_column("datasets", "source_type")
