"""merge heads for v3.3

Revision ID: 9272d97dee04
Revises: 3f4a86cac509, c9d1e2f3a4b5
Create Date: 2026-08-10 11:32:43.381790

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9272d97dee04'
down_revision: Union[str, Sequence[str], None] = ('3f4a86cac509', 'c9d1e2f3a4b5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
