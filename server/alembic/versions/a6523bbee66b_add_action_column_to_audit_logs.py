"""add action column to audit_logs

Revision ID: a6523bbee66b
Revises: f979e65675e6
Create Date: 2026-08-11 10:05:43.847955

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a6523bbee66b'
down_revision: Union[str, Sequence[str], None] = 'f979e65675e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'audit_logs' not in tables:
        op.create_table(
            'audit_logs',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('request_id', sa.String(length=128), nullable=False),
            sa.Column('user_id', sa.String(length=36), nullable=True),
            sa.Column('method', sa.String(length=16), nullable=False),
            sa.Column('path', sa.String(length=1024), nullable=False),
            sa.Column('status_code', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('duration_ms', sa.Float(), nullable=False, server_default='0.0'),
            sa.Column('ip_address', sa.String(length=128), nullable=True),
            sa.Column('user_agent', sa.Text(), nullable=True),
            sa.Column('action', sa.String(length=128), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_audit_logs_user_created_at', 'audit_logs', ['user_id', 'created_at'])
        op.create_index('ix_audit_logs_request_id', 'audit_logs', ['request_id'], unique=True)
        op.create_index('ix_audit_logs_action', 'audit_logs', ['action'], unique=False)
    else:
        columns = [c['name'] for c in inspector.get_columns('audit_logs')]
        if 'action' not in columns:
            op.add_column('audit_logs', sa.Column('action', sa.String(length=128), nullable=True))
            op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'audit_logs' in tables:
        columns = [c['name'] for c in inspector.get_columns('audit_logs')]
        if 'action' in columns:
            op.drop_index(op.f('ix_audit_logs_action'), table_name='audit_logs')
            op.drop_column('audit_logs', 'action')
