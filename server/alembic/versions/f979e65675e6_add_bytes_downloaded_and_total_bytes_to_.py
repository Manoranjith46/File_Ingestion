"""add bytes_downloaded and total_bytes to async_ingestion_jobs

Revision ID: f979e65675e6
Revises: 9272d97dee04
Create Date: 2026-08-10 11:33:06.589353

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f979e65675e6'
down_revision: Union[str, Sequence[str], None] = '9272d97dee04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add bytes_downloaded and total_bytes to async_ingestion_jobs."""
    op.add_column(
        'async_ingestion_jobs',
        sa.Column('bytes_downloaded', sa.BigInteger(), nullable=False, server_default='0'),
    )
    op.add_column(
        'async_ingestion_jobs',
        sa.Column('total_bytes', sa.BigInteger(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Remove bytes_downloaded and total_bytes from async_ingestion_jobs."""
    op.drop_column('async_ingestion_jobs', 'total_bytes')
    op.drop_column('async_ingestion_jobs', 'bytes_downloaded')
