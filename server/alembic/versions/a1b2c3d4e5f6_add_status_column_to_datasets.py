"""add status column to datasets

Revision ID: a1b2c3d4e5f6
Revises: 9ab1c7d4e2f3
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9ab1c7d4e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


import sqlalchemy as sa


def upgrade() -> None:
    """Ensure the datasets.status column exists on existing databases."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        try:
            op.add_column("datasets", sa.Column("status", sa.String(50), nullable=False, server_default="created"))
        except Exception:
            pass
    else:
        op.execute(
            """
            ALTER TABLE datasets
            ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'created'
            """
        )


def downgrade() -> None:
    """Remove the datasets.status column if it exists."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.execute("ALTER TABLE datasets DROP COLUMN IF EXISTS status")
