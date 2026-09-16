"""add_organizations_and_memberships

Revision ID: 3c0364a899e1
Revises: ee4052d195c3
Create Date: 2026-09-15 17:22:42.059882

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3c0364a899e1'
down_revision: Union[str, Sequence[str], None] = 'ee4052d195c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to add organizations and organization memberships."""
    # 1. Create organizations table
    op.create_table(
        'organizations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('auth0_org_id', sa.String(length=128), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('google_sso_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_organizations_auth0_org_id'), 'organizations', ['auth0_org_id'], unique=True)
    op.create_index(op.f('ix_organizations_name'), 'organizations', ['name'], unique=True)

    # 2. Create organization_memberships table
    op.create_table(
        'organization_memberships',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=50), nullable=False, server_default=sa.text("'member'")),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'user_id', name='uq_org_user_membership')
    )
    op.create_index(op.f('ix_organization_memberships_organization_id'), 'organization_memberships', ['organization_id'], unique=False)
    op.create_index(op.f('ix_organization_memberships_user_id'), 'organization_memberships', ['user_id'], unique=False)

    # 3. Add organization_id to datasets and ingested_files
    op.add_column('datasets', sa.Column('organization_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_datasets_organization_id'), 'datasets', ['organization_id'], unique=False)
    op.create_foreign_key('fk_datasets_organization_id', 'datasets', 'organizations', ['organization_id'], ['id'], ondelete='CASCADE')

    op.add_column('ingested_files', sa.Column('organization_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_ingested_files_organization_id'), 'ingested_files', ['organization_id'], unique=False)
    op.create_foreign_key('fk_ingested_files_organization_id', 'ingested_files', 'organizations', ['organization_id'], ['id'], ondelete='CASCADE')

    # 4. Add auth0_sub and is_active to users
    op.add_column('users', sa.Column('auth0_sub', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    op.create_index(op.f('ix_users_auth0_sub'), 'users', ['auth0_sub'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_users_auth0_sub'), table_name='users')
    op.drop_column('users', 'is_active')
    op.drop_column('users', 'auth0_sub')

    op.drop_constraint('ingested_files_organization_id_fkey', 'ingested_files', type_='foreignkey')
    op.drop_index(op.f('ix_ingested_files_organization_id'), table_name='ingested_files')
    op.drop_column('ingested_files', 'organization_id')

    op.drop_constraint('datasets_organization_id_fkey', 'datasets', type_='foreignkey')
    op.drop_index(op.f('ix_datasets_organization_id'), table_name='datasets')
    op.drop_column('datasets', 'organization_id')

    op.drop_index(op.f('ix_organization_memberships_user_id'), table_name='organization_memberships')
    op.drop_index(op.f('ix_organization_memberships_organization_id'), table_name='organization_memberships')
    op.drop_table('organization_memberships')

    op.drop_index(op.f('ix_organizations_name'), table_name='organizations')
    op.drop_index(op.f('ix_organizations_auth0_org_id'), table_name='organizations')
    op.drop_table('organizations')
    # ### end Alembic commands ###
