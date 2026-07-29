"""Add v3 cloud ingestion tables and user refresh token columns.

Revision ID: b3c4d5e6f7a8
Revises: 74769e1c95ef
Create Date: 2026-07-29 14:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = '74769e1c95ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add refresh token columns to users table
    op.add_column('users', sa.Column('google_refresh_token', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('microsoft_refresh_token', sa.Text(), nullable=True))

    # 2. Create provider_hash_mappings table (Rosetta Stone)
    op.create_table(
        'provider_hash_mappings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('provider_name', sa.String(length=50), nullable=False),
        sa.Column('provider_file_id', sa.String(length=255), nullable=False),
        sa.Column('provider_hash', sa.String(length=255), nullable=False),
        sa.Column('master_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['master_hash'], ['uploaded_files.master_hash'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_name', 'provider_hash', name='uq_provider_name_hash')
    )
    op.create_index('ix_provider_hash_mappings_provider_name', 'provider_hash_mappings', ['provider_name'])
    op.create_index('ix_provider_hash_mappings_provider_file_id', 'provider_hash_mappings', ['provider_file_id'])
    op.create_index('ix_provider_hash_mappings_provider_hash', 'provider_hash_mappings', ['provider_hash'])
    op.create_index('ix_provider_hash_mappings_master_hash', 'provider_hash_mappings', ['master_hash'])

    # 3. Create async_ingestion_jobs table (Job Ledger)
    op.create_table(
        'async_ingestion_jobs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('dataset_id', sa.String(length=36), nullable=False),
        sa.Column('folder_id', sa.String(length=36), nullable=True),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('source_url_or_id', sa.String(length=500), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='pending', nullable=False),
        sa.Column('progress_percentage', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('master_hash', sa.String(length=64), nullable=True),
        sa.Column('file_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['folder_id'], ['folders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['file_id'], ['uploaded_files.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_async_ingestion_jobs_user_id', 'async_ingestion_jobs', ['user_id'])
    op.create_index('ix_async_ingestion_jobs_dataset_id', 'async_ingestion_jobs', ['dataset_id'])
    op.create_index('ix_async_ingestion_jobs_folder_id', 'async_ingestion_jobs', ['folder_id'])
    op.create_index('ix_async_ingestion_jobs_status', 'async_ingestion_jobs', ['status'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_async_ingestion_jobs_status', table_name='async_ingestion_jobs')
    op.drop_index('ix_async_ingestion_jobs_folder_id', table_name='async_ingestion_jobs')
    op.drop_index('ix_async_ingestion_jobs_dataset_id', table_name='async_ingestion_jobs')
    op.drop_index('ix_async_ingestion_jobs_user_id', table_name='async_ingestion_jobs')
    op.drop_table('async_ingestion_jobs')

    op.drop_index('ix_provider_hash_mappings_master_hash', table_name='provider_hash_mappings')
    op.drop_index('ix_provider_hash_mappings_provider_hash', table_name='provider_hash_mappings')
    op.drop_index('ix_provider_hash_mappings_provider_file_id', table_name='provider_hash_mappings')
    op.drop_index('ix_provider_hash_mappings_provider_name', table_name='provider_hash_mappings')
    op.drop_table('provider_hash_mappings')

    op.drop_column('users', 'microsoft_refresh_token')
    op.drop_column('users', 'google_refresh_token')
