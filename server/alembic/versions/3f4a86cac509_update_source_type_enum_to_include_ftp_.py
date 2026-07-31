"""Update source_type enum to include FTP, Local, GDrive, Sharepoint

Revision ID: 3f4a86cac509
Revises: b3c4d5e6f7a8
Create Date: 2026-07-31 10:16:40.573328

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f4a86cac509'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Update source_type enum to include FTP, Local, GDrive, Sharepoint"""
    # 1. Commit the active Alembic transaction so we can alter the ENUM
    op.execute("COMMIT")
    
    # 2. Add values safely using the exact ENUM name from the database
    op.execute("ALTER TYPE uploaded_file_source_type ADD VALUE IF NOT EXISTS 'FTP'")
    op.execute("ALTER TYPE uploaded_file_source_type ADD VALUE IF NOT EXISTS 'Local'")
    op.execute("ALTER TYPE uploaded_file_source_type ADD VALUE IF NOT EXISTS 'GDrive'")
    op.execute("ALTER TYPE uploaded_file_source_type ADD VALUE IF NOT EXISTS 'Sharepoint'")


def downgrade() -> None:
    """Downgrade schema."""
    pass