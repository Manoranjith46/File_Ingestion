"""Add source_type to uploaded_files.

Revision ID: 74769e1c95ef
Revises: a1b2c3d4e5f6
Create Date: 2026-07-28 19:19:40.780690

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '74769e1c95ef'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    enum = postgresql.ENUM(
        'FTP',
        'GDrive',
        'Sharepoint',
        name='uploaded_file_source_type',
        create_type=False,
    )
    enum.create(bind, checkfirst=True)

    op.add_column(
        'uploaded_files',
        sa.Column('source_type', enum, nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('uploaded_files', 'source_type')

    postgresql.ENUM(
        'FTP',
        'GDrive',
        'Sharepoint',
        name='uploaded_file_source_type',
    ).drop(op.get_bind(), checkfirst=True)

