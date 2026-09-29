"""Search-run tracking (M5.4/M5.8/M5.9): source attempts and claim times, per-source filter
reports, per-result unverified filters, one active run per search, runs-per-hour index.

Revision ID: 008e13f9596a
Revises: af0f71f9d25c
Create Date: 2026-09-28 01:22:24.275491

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '008e13f9596a'
down_revision: str | Sequence[str] | None = 'af0f71f9d25c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('search_job_results', sa.Column('unverified_filters', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False))
    op.add_column('search_job_sources', sa.Column('attempts', sa.SmallInteger(), server_default=sa.text('0'), nullable=False))
    op.add_column('search_job_sources', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('search_job_sources', sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('search_job_sources', sa.Column('filter_report', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False))
    # Autogenerate doesn't detect CHECK constraints: added by hand.
    op.create_check_constraint(
        op.f('ck_search_job_sources_attempts_non_negative'), 'search_job_sources', 'attempts >= 0'
    )
    op.create_index('ix_search_jobs_user_id_created_at', 'search_jobs', ['user_id', 'created_at'], unique=False)
    op.create_index('uq_search_jobs_active_search_id', 'search_jobs', ['search_id'], unique=True, postgresql_where=sa.text("status IN ('pending', 'running')"))


def downgrade() -> None:
    op.drop_index('uq_search_jobs_active_search_id', table_name='search_jobs', postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.drop_index('ix_search_jobs_user_id_created_at', table_name='search_jobs')
    op.drop_constraint(
        op.f('ck_search_job_sources_attempts_non_negative'), 'search_job_sources', type_='check'
    )
    op.drop_column('search_job_sources', 'filter_report')
    op.drop_column('search_job_sources', 'finished_at')
    op.drop_column('search_job_sources', 'started_at')
    op.drop_column('search_job_sources', 'attempts')
    op.drop_column('search_job_results', 'unverified_filters')
