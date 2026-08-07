"""Add audit_logs table for immutable request audit events.

Revision ID: c9d1e2f3a4b5
Revises: b3c4d5e6f7a8
Create Date: 2026-08-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_logs_user_created_at', 'audit_logs', ['user_id', 'created_at'])
    op.create_index('ix_audit_logs_request_id', 'audit_logs', ['request_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_audit_logs_request_id', table_name='audit_logs')
    op.drop_index('ix_audit_logs_user_created_at', table_name='audit_logs')
    op.drop_table('audit_logs')
