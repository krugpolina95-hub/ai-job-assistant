"""Initial MVP schema: users, profiles, skills, sources, vacancies, searches,
search jobs, matches, saved vacancies, history and AI interaction metadata.

Revision ID: ff0fd9cabbe9
Revises:
Create Date: 2026-09-27 21:54:34.069398

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'ff0fd9cabbe9'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # citext: case-insensitive users.email. pg_trgm: fuzzy title dedup (TD-16).
    # Both are trusted extensions, so the database owner can create them.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table('skills',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('normalized_name', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_skills')),
    sa.UniqueConstraint('normalized_name', name=op.f('uq_skills_normalized_name'))
    )
    op.create_table('sources',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('adapter_key', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('base_url', sa.Text(), nullable=False),
    sa.Column('source_type', sa.Text(), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('terms_url', sa.Text(), nullable=True),
    sa.Column('rate_limit', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('storage_policy', sa.Text(), server_default='none', nullable=False),
    sa.Column('retention_days', sa.Integer(), nullable=True),
    sa.Column('attribution_text', sa.Text(), nullable=True),
    sa.Column('compliance_reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('compliance_notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source_type IN ('api', 'rss', 'ats')", name=op.f('ck_sources_source_type')),
    sa.CheckConstraint("storage_policy IN ('full', 'snippet', 'none')", name=op.f('ck_sources_storage_policy')),
    sa.CheckConstraint('NOT is_enabled OR compliance_reviewed_at IS NOT NULL', name=op.f('ck_sources_enabled_requires_review')),
    sa.CheckConstraint('retention_days > 0', name=op.f('ck_sources_retention_days_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sources')),
    sa.UniqueConstraint('adapter_key', name=op.f('uq_sources_adapter_key'))
    )
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('is_verified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('preferred_locale', sa.Text(), server_default='en', nullable=False),
    sa.Column('timezone', sa.Text(), server_default='UTC', nullable=False),
    sa.Column('terms_version', sa.Text(), nullable=True),
    sa.Column('privacy_version', sa.Text(), nullable=True),
    sa.Column('terms_accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failed_login_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('char_length(email) <= 254', name=op.f('ck_users_email_length')),
    sa.CheckConstraint('failed_login_count >= 0', name=op.f('ck_users_failed_login_count_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('searches',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('original_text', sa.Text(), nullable=True),
    sa.Column('normalized_params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('params_schema_version', sa.SmallInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='draft', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("channel IN ('form', 'ai')", name=op.f('ck_searches_channel')),
    sa.CheckConstraint("status IN ('draft', 'confirmed', 'archived')", name=op.f('ck_searches_status')),
    sa.CheckConstraint('char_length(original_text) <= 2000', name=op.f('ck_searches_original_text_length')),
    sa.CheckConstraint('params_schema_version > 0', name=op.f('ck_searches_params_schema_version_positive')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_searches_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_searches'))
    )
    op.create_index('ix_searches_user_id_created_at', 'searches', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_table('skill_aliases',
    sa.Column('alias_normalized', sa.Text(), nullable=False),
    sa.Column('skill_id', sa.Uuid(), nullable=False),
    sa.Column('locale', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['skill_id'], ['skills.id'], name=op.f('fk_skill_aliases_skill_id_skills'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('alias_normalized', name=op.f('pk_skill_aliases'))
    )
    op.create_index(op.f('ix_skill_aliases_skill_id'), 'skill_aliases', ['skill_id'], unique=False)
    op.create_table('user_profiles',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('desired_roles', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.Column('region', sa.Text(), nullable=True),
    sa.Column('city', sa.Text(), nullable=True),
    sa.Column('location_source', sa.Text(), nullable=True),
    sa.Column('salary_min', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('salary_max', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('salary_currency', sa.String(length=3), nullable=True),
    sa.Column('salary_period', sa.Text(), nullable=True),
    sa.Column('work_formats', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('employment_types', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('experience_level', sa.Text(), nullable=True),
    sa.Column('languages', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('relocation_ready', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('ai_use_profile', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('ai_use_history', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name=op.f('ck_user_profiles_country_iso3166')),
    sa.CheckConstraint("experience_level IN ('intern', 'junior', 'middle', 'senior', 'lead')", name=op.f('ck_user_profiles_experience_level')),
    sa.CheckConstraint("location_source IN ('manual', 'ip', 'browser')", name=op.f('ck_user_profiles_location_source')),
    sa.CheckConstraint("salary_currency ~ '^[A-Z]{3}$'", name=op.f('ck_user_profiles_salary_currency_iso4217')),
    sa.CheckConstraint("salary_period IN ('hour', 'day', 'month', 'year', 'unknown')", name=op.f('ck_user_profiles_salary_period')),
    sa.CheckConstraint('char_length(summary) <= 5000', name=op.f('ck_user_profiles_summary_length')),
    sa.CheckConstraint('salary_min >= 0', name=op.f('ck_user_profiles_salary_min_non_negative')),
    sa.CheckConstraint('salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max', name=op.f('ck_user_profiles_salary_min_le_salary_max')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_profiles_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_profiles')),
    sa.UniqueConstraint('user_id', name=op.f('uq_user_profiles_user_id'))
    )
    op.create_table('user_skills',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('skill_id', sa.Uuid(), nullable=False),
    sa.Column('skill_level', sa.Text(), nullable=True),
    sa.Column('years_experience', sa.Numeric(precision=3, scale=1), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("skill_level IN ('beginner', 'intermediate', 'advanced', 'expert')", name=op.f('ck_user_skills_skill_level')),
    sa.CheckConstraint('years_experience >= 0 AND years_experience <= 70', name=op.f('ck_user_skills_years_experience_range')),
    sa.ForeignKeyConstraint(['skill_id'], ['skills.id'], name=op.f('fk_user_skills_skill_id_skills'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_skills_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'skill_id', name=op.f('pk_user_skills'))
    )
    op.create_index(op.f('ix_user_skills_skill_id'), 'user_skills', ['skill_id'], unique=False)
    op.create_table('vacancies',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('external_id', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('canonical_url', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('normalized_title', sa.Text(), nullable=False),
    sa.Column('company', sa.Text(), nullable=True),
    sa.Column('company_normalized', sa.Text(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('description_snippet', sa.Text(), nullable=True),
    sa.Column('requirements', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('responsibilities', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('skills', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('language', sa.Text(), nullable=True),
    sa.Column('salary_min', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('salary_max', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('salary_currency', sa.String(length=3), nullable=True),
    sa.Column('salary_period', sa.Text(), nullable=True),
    sa.Column('salary_raw', sa.Text(), nullable=True),
    sa.Column('salary_monthly_min_eur', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('salary_monthly_max_eur', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.Column('region', sa.Text(), nullable=True),
    sa.Column('city', sa.Text(), nullable=True),
    sa.Column('location_raw', sa.Text(), nullable=True),
    sa.Column('remote_type', sa.Text(), nullable=True),
    sa.Column('employment_type', sa.Text(), nullable=True),
    sa.Column('experience_level', sa.Text(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('collected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('duplicate_group_id', sa.Uuid(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("canonical_url ~ '^https?://'", name=op.f('ck_vacancies_canonical_url_http')),
    sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name=op.f('ck_vacancies_country_iso3166')),
    sa.CheckConstraint("employment_type IN ('full_time', 'part_time', 'contract', 'internship', 'temporary')", name=op.f('ck_vacancies_employment_type')),
    sa.CheckConstraint("experience_level IN ('intern', 'junior', 'middle', 'senior', 'lead')", name=op.f('ck_vacancies_experience_level')),
    sa.CheckConstraint("remote_type IN ('remote', 'hybrid', 'onsite')", name=op.f('ck_vacancies_remote_type')),
    sa.CheckConstraint("salary_currency ~ '^[A-Z]{3}$'", name=op.f('ck_vacancies_salary_currency_iso4217')),
    sa.CheckConstraint("salary_period IN ('hour', 'day', 'month', 'year', 'unknown')", name=op.f('ck_vacancies_salary_period')),
    sa.CheckConstraint("url ~ '^https?://'", name=op.f('ck_vacancies_url_http')),
    sa.CheckConstraint('char_length(content_hash) = 64', name=op.f('ck_vacancies_content_hash_sha256')),
    sa.CheckConstraint('char_length(description_snippet) <= 500', name=op.f('ck_vacancies_description_snippet_length')),
    sa.CheckConstraint('is_active = (removed_at IS NULL)', name=op.f('ck_vacancies_active_matches_removed_at')),
    sa.CheckConstraint('salary_min >= 0', name=op.f('ck_vacancies_salary_min_non_negative')),
    sa.CheckConstraint('salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max', name=op.f('ck_vacancies_salary_min_le_salary_max')),
    sa.CheckConstraint('salary_monthly_min_eur IS NULL OR salary_monthly_max_eur IS NULL OR salary_monthly_min_eur <= salary_monthly_max_eur', name=op.f('ck_vacancies_salary_monthly_min_eur_le_salary_monthly_max_eur')),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_vacancies_source_id_sources'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vacancies'))
    )
    op.create_index(op.f('ix_vacancies_canonical_url'), 'vacancies', ['canonical_url'], unique=False)
    op.create_index(op.f('ix_vacancies_content_hash'), 'vacancies', ['content_hash'], unique=False)
    op.create_index(op.f('ix_vacancies_duplicate_group_id'), 'vacancies', ['duplicate_group_id'], unique=False)
    op.create_index('ix_vacancies_normalized_title_trgm', 'vacancies', ['normalized_title'], unique=False, postgresql_using='gin', postgresql_ops={'normalized_title': 'gin_trgm_ops'})
    op.create_index('ix_vacancies_skills_gin', 'vacancies', ['skills'], unique=False, postgresql_using='gin')
    op.create_index(op.f('ix_vacancies_source_id'), 'vacancies', ['source_id'], unique=False)
    op.create_index('uq_vacancies_source_id_external_id', 'vacancies', ['source_id', 'external_id'], unique=True, postgresql_where=sa.text('external_id IS NOT NULL'))
    op.create_table('ai_interactions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('search_id', sa.Uuid(), nullable=True),
    sa.Column('task', sa.Text(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('cost_estimate_usd', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('succeeded', 'failed')", name=op.f('ck_ai_interactions_status')),
    sa.CheckConstraint("task IN ('parse_query', 'clarify', 'explain_match')", name=op.f('ck_ai_interactions_task')),
    sa.CheckConstraint('cost_estimate_usd >= 0', name=op.f('ck_ai_interactions_cost_estimate_usd_non_negative')),
    sa.CheckConstraint('input_tokens >= 0', name=op.f('ck_ai_interactions_input_tokens_non_negative')),
    sa.CheckConstraint('latency_ms >= 0', name=op.f('ck_ai_interactions_latency_ms_non_negative')),
    sa.CheckConstraint('output_tokens >= 0', name=op.f('ck_ai_interactions_output_tokens_non_negative')),
    sa.ForeignKeyConstraint(['search_id'], ['searches.id'], name=op.f('fk_ai_interactions_search_id_searches'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_ai_interactions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_interactions'))
    )
    op.create_index(op.f('ix_ai_interactions_search_id'), 'ai_interactions', ['search_id'], unique=False)
    op.create_index('ix_ai_interactions_user_id_created_at', 'ai_interactions', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_table('saved_vacancies',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('vacancy_id', sa.Uuid(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('char_length(note) <= 2000', name=op.f('ck_saved_vacancies_note_length')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_saved_vacancies_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_saved_vacancies_vacancy_id_vacancies'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_saved_vacancies')),
    sa.UniqueConstraint('user_id', 'vacancy_id', name=op.f('uq_saved_vacancies_user_id_vacancy_id'))
    )
    op.create_index('ix_saved_vacancies_user_id_created_at', 'saved_vacancies', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index(op.f('ix_saved_vacancies_vacancy_id'), 'saved_vacancies', ['vacancy_id'], unique=False)
    op.create_table('search_history_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('search_id', sa.Uuid(), nullable=True),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("event_type IN ('search_created', 'search_confirmed', 'search_run')", name=op.f('ck_search_history_events_event_type')),
    sa.ForeignKeyConstraint(['search_id'], ['searches.id'], name=op.f('fk_search_history_events_search_id_searches'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_search_history_events_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_search_history_events'))
    )
    op.create_index(op.f('ix_search_history_events_search_id'), 'search_history_events', ['search_id'], unique=False)
    op.create_index('ix_search_history_events_user_id_created_at', 'search_history_events', ['user_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_table('search_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('search_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('idempotency_key', sa.Text(), nullable=True),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('progress', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('result_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'partially_completed', 'failed', 'cancelled')", name=op.f('ck_search_jobs_status')),
    sa.CheckConstraint('finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at', name=op.f('ck_search_jobs_finished_after_started')),
    sa.CheckConstraint('result_count >= 0', name=op.f('ck_search_jobs_result_count_non_negative')),
    sa.ForeignKeyConstraint(['search_id'], ['searches.id'], name=op.f('fk_search_jobs_search_id_searches'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_search_jobs_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_search_jobs')),
    sa.UniqueConstraint('user_id', 'idempotency_key', name=op.f('uq_search_jobs_user_id_idempotency_key'))
    )
    op.create_index('ix_search_jobs_active_deadline_at', 'search_jobs', ['deadline_at'], unique=False, postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.create_index('ix_search_jobs_active_user_id', 'search_jobs', ['user_id'], unique=False, postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.create_index('ix_search_jobs_search_id_created_at', 'search_jobs', ['search_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_table('search_job_results',
    sa.Column('search_job_id', sa.Uuid(), nullable=False),
    sa.Column('vacancy_id', sa.Uuid(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('match_score', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('match_score >= 0 AND match_score <= 1', name=op.f('ck_search_job_results_match_score_range')),
    sa.CheckConstraint('rank > 0', name=op.f('ck_search_job_results_rank_positive')),
    sa.ForeignKeyConstraint(['search_job_id'], ['search_jobs.id'], name=op.f('fk_search_job_results_search_job_id_search_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_search_job_results_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('search_job_id', 'vacancy_id', name=op.f('pk_search_job_results'))
    )
    op.create_index(op.f('ix_search_job_results_search_job_id_rank'), 'search_job_results', ['search_job_id', 'rank'], unique=False)
    op.create_index(op.f('ix_search_job_results_vacancy_id'), 'search_job_results', ['vacancy_id'], unique=False)
    op.create_table('search_job_sources',
    sa.Column('search_job_id', sa.Uuid(), nullable=False),
    sa.Column('source_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('fetched_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('stored_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'timed_out', 'skipped')", name=op.f('ck_search_job_sources_status')),
    sa.CheckConstraint('duration_ms >= 0', name=op.f('ck_search_job_sources_duration_ms_non_negative')),
    sa.CheckConstraint('fetched_count >= 0', name=op.f('ck_search_job_sources_fetched_count_non_negative')),
    sa.CheckConstraint('stored_count >= 0', name=op.f('ck_search_job_sources_stored_count_non_negative')),
    sa.ForeignKeyConstraint(['search_job_id'], ['search_jobs.id'], name=op.f('fk_search_job_sources_search_job_id_search_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_search_job_sources_source_id_sources'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('search_job_id', 'source_id', name=op.f('pk_search_job_sources'))
    )
    op.create_index(op.f('ix_search_job_sources_source_id'), 'search_job_sources', ['source_id'], unique=False)
    op.create_table('vacancy_matches',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('search_job_id', sa.Uuid(), nullable=False),
    sa.Column('vacancy_id', sa.Uuid(), nullable=False),
    sa.Column('match_level', sa.Text(), nullable=False),
    sa.Column('match_score', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('factors', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('matching_skills', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('missing_skills', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('explanation', sa.Text(), nullable=True),
    sa.Column('explanation_locale', sa.Text(), nullable=True),
    sa.Column('explanation_source', sa.Text(), nullable=True),
    sa.Column('algorithm_version', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("explanation_source IN ('template', 'ai')", name=op.f('ck_vacancy_matches_explanation_source')),
    sa.CheckConstraint("match_level IN ('high', 'medium', 'low')", name=op.f('ck_vacancy_matches_match_level')),
    sa.CheckConstraint('algorithm_version > 0', name=op.f('ck_vacancy_matches_algorithm_version_positive')),
    sa.CheckConstraint('match_score >= 0 AND match_score <= 1', name=op.f('ck_vacancy_matches_match_score_range')),
    sa.ForeignKeyConstraint(['search_job_id'], ['search_jobs.id'], name=op.f('fk_vacancy_matches_search_job_id_search_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_vacancy_matches_user_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_vacancy_matches_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vacancy_matches')),
    sa.UniqueConstraint('search_job_id', 'vacancy_id', name=op.f('uq_vacancy_matches_search_job_id_vacancy_id'))
    )
    op.create_index('ix_vacancy_matches_user_id_vacancy_id_created_at', 'vacancy_matches', ['user_id', 'vacancy_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index(op.f('ix_vacancy_matches_vacancy_id'), 'vacancy_matches', ['vacancy_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_vacancy_matches_vacancy_id'), table_name='vacancy_matches')
    op.drop_index('ix_vacancy_matches_user_id_vacancy_id_created_at', table_name='vacancy_matches')
    op.drop_table('vacancy_matches')
    op.drop_index(op.f('ix_search_job_sources_source_id'), table_name='search_job_sources')
    op.drop_table('search_job_sources')
    op.drop_index(op.f('ix_search_job_results_vacancy_id'), table_name='search_job_results')
    op.drop_index(op.f('ix_search_job_results_search_job_id_rank'), table_name='search_job_results')
    op.drop_table('search_job_results')
    op.drop_index('ix_search_jobs_search_id_created_at', table_name='search_jobs')
    op.drop_index('ix_search_jobs_active_user_id', table_name='search_jobs', postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.drop_index('ix_search_jobs_active_deadline_at', table_name='search_jobs', postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.drop_table('search_jobs')
    op.drop_index('ix_search_history_events_user_id_created_at', table_name='search_history_events')
    op.drop_index(op.f('ix_search_history_events_search_id'), table_name='search_history_events')
    op.drop_table('search_history_events')
    op.drop_index(op.f('ix_saved_vacancies_vacancy_id'), table_name='saved_vacancies')
    op.drop_index('ix_saved_vacancies_user_id_created_at', table_name='saved_vacancies')
    op.drop_table('saved_vacancies')
    op.drop_index('ix_ai_interactions_user_id_created_at', table_name='ai_interactions')
    op.drop_index(op.f('ix_ai_interactions_search_id'), table_name='ai_interactions')
    op.drop_table('ai_interactions')
    op.drop_index('uq_vacancies_source_id_external_id', table_name='vacancies', postgresql_where=sa.text('external_id IS NOT NULL'))
    op.drop_index(op.f('ix_vacancies_source_id'), table_name='vacancies')
    op.drop_index('ix_vacancies_skills_gin', table_name='vacancies', postgresql_using='gin')
    op.drop_index('ix_vacancies_normalized_title_trgm', table_name='vacancies', postgresql_using='gin', postgresql_ops={'normalized_title': 'gin_trgm_ops'})
    op.drop_index(op.f('ix_vacancies_duplicate_group_id'), table_name='vacancies')
    op.drop_index(op.f('ix_vacancies_content_hash'), table_name='vacancies')
    op.drop_index(op.f('ix_vacancies_canonical_url'), table_name='vacancies')
    op.drop_table('vacancies')
    op.drop_index(op.f('ix_user_skills_skill_id'), table_name='user_skills')
    op.drop_table('user_skills')
    op.drop_table('user_profiles')
    op.drop_index(op.f('ix_skill_aliases_skill_id'), table_name='skill_aliases')
    op.drop_table('skill_aliases')
    op.drop_index('ix_searches_user_id_created_at', table_name='searches')
    op.drop_table('searches')
    op.drop_table('users')
    op.drop_table('sources')
    op.drop_table('skills')
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS citext")
