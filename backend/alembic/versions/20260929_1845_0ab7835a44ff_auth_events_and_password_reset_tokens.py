"""Auth events and password reset tokens (M3.5/M3.6): security event log for brute-force
protection (AUTH-006) and single-use password reset tokens stored as hashes (AUTH-004).

Revision ID: 0ab7835a44ff
Revises: 008e13f9596a
Create Date: 2026-09-29 18:45:12.512744

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0ab7835a44ff'
down_revision: str | Sequence[str] | None = '008e13f9596a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('auth_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('email_hash', sa.Text(), nullable=True),
    sa.Column('ip_prefix', sa.Text(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("event_type IN ('login_succeeded', 'login_failed', 'account_locked', 'rate_limited', 'password_changed', 'password_reset_requested', 'password_reset')", name=op.f('ck_auth_events_event_type')),
    sa.CheckConstraint('char_length(detail) <= 64', name=op.f('ck_auth_events_detail_length')),
    sa.CheckConstraint('char_length(email_hash) <= 64', name=op.f('ck_auth_events_email_hash_length')),
    sa.CheckConstraint('char_length(ip_prefix) <= 64', name=op.f('ck_auth_events_ip_prefix_length')),
    sa.CheckConstraint('char_length(user_agent) <= 512', name=op.f('ck_auth_events_user_agent_length')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_auth_events_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_events'))
    )
    op.create_index(op.f('ix_auth_events_created_at'), 'auth_events', ['created_at'], unique=False)
    op.create_index(op.f('ix_auth_events_user_id_created_at'), 'auth_events', ['user_id', 'created_at'], unique=False)
    op.create_table('password_reset_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('char_length(token_hash) = 64', name=op.f('ck_password_reset_tokens_token_hash_sha256')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_password_reset_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_password_reset_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_password_reset_tokens_token_hash'))
    )
    op.create_index(op.f('ix_password_reset_tokens_user_id'), 'password_reset_tokens', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_password_reset_tokens_user_id'), table_name='password_reset_tokens')
    op.drop_table('password_reset_tokens')
    op.drop_index(op.f('ix_auth_events_user_id_created_at'), table_name='auth_events')
    op.drop_index(op.f('ix_auth_events_created_at'), table_name='auth_events')
    op.drop_table('auth_events')
