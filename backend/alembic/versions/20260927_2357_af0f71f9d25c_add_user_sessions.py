"""Add user_sessions: opaque login sessions stored as token hashes (TD-03, M3).

Revision ID: af0f71f9d25c
Revises: ff0fd9cabbe9
Create Date: 2026-09-27 23:57:10.533538

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'af0f71f9d25c'
down_revision: str | Sequence[str] | None = 'ff0fd9cabbe9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('user_sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('char_length(token_hash) = 64', name=op.f('ck_user_sessions_token_hash_sha256')),
    sa.CheckConstraint('char_length(user_agent) <= 512', name=op.f('ck_user_sessions_user_agent_length')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_sessions_token_hash'))
    )
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_table('user_sessions')
