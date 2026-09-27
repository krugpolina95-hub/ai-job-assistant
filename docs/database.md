# Database Schema

PostgreSQL 16. The schema is owned by Alembic migrations in `backend/alembic/versions/`. ORM models live in `backend/app/models/`. This document describes the schema at revision **`af0f71f9d25c`** (`ff0fd9cabbe9` initial MVP schema + `af0f71f9d25c` user sessions).

Related: `architecture-plan.md` §7 (data architecture), TD-12 (idempotency), TD-13 (results), TD-14 (salary), TD-16 (dedup), TD-23 (storage policy), TD-24 (deletion), TD-26 (IDs).

## Conventions

| Topic | Rule |
|---|---|
| Primary keys | `uuid`, generated in the app as UUIDv7 (`app/core/ids.py`) for time ordering and index locality. Link tables use composite PKs. |
| Timestamps | `timestamptz` (UTC). `created_at` defaults to `now()`. `updated_at` defaults to `now()` and is refreshed by the ORM on every update. No triggers. |
| Enums | `text` + a CHECK constraint named `ck_<table>_<column>`, mirrored by a Python `StrEnum` in `app/core/enums.py`. The ORM type `StrEnumType` returns enum members. Adding a value means updating the enum and the CHECK in a migration. |
| JSON | `jsonb`. Lists default to `'[]'`, objects to `'{}'`. Shapes are validated by Pydantic, not the database. |
| Money | `numeric(12,2)` with an ISO 4217 currency code (`^[A-Z]{3}$`). Never floats. |
| Countries | ISO 3166-1 alpha-2 (`^[A-Z]{2}$`). |
| Unknown data | `NULL`. Vacancy fields a source didn't provide are never filled in or guessed (VACANCY-003). |
| Constraint names | Deterministic naming convention: `pk_`, `fk_<table>_<column>_<ref>`, `uq_`, `ix_`, `ck_`. |
| Foreign keys | Every FK has an index (enforced by `tests/unit/test_model_metadata.py`). User-owned rows use `ON DELETE CASCADE` from `users`. Shared rows use `RESTRICT`. |
| Relationships | ORM relationships are `lazy="raise"`: services must load related rows explicitly (`selectinload`). DB-level cascades are trusted (`passive_deletes=True`). |
| Extensions | `citext` (case-insensitive email), `pg_trgm` (fuzzy title dedup). Both are created by the initial migration. |

No business logic lives in the database. Constraints guard invariants that must never be violated, whichever code path writes the row.

## Entity-relationship overview

```mermaid
erDiagram
  users ||--o| user_profiles : has
  users ||--o{ user_skills : has
  skills ||--o{ user_skills : ""
  skills ||--o{ skill_aliases : ""
  users ||--o{ searches : owns
  searches ||--o{ search_jobs : runs
  users ||--o{ search_jobs : owns
  search_jobs ||--o{ search_job_sources : per_source
  sources ||--o{ search_job_sources : ""
  search_jobs ||--o{ search_job_results : yields
  vacancies ||--o{ search_job_results : ""
  sources ||--o{ vacancies : provides
  search_jobs ||--o{ vacancy_matches : scores
  vacancies ||--o{ vacancy_matches : ""
  users ||--o{ vacancy_matches : owns
  users ||--o{ saved_vacancies : saves
  vacancies ||--o{ saved_vacancies : ""
  users ||--o{ search_history_events : ""
  searches ||--o{ search_history_events : ""
  users ||--o{ ai_interactions : ""
  searches |o--o{ ai_interactions : ""
```

Shared data (not owned by any user): `skills`, `skill_aliases`, `sources`, `vacancies`.

## Tables

Columns marked **NN** are `NOT NULL`. Every table with `updated_at` also has `created_at`.

### users
Account identity. Login sessions live in `user_sessions`.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| email | citext **NN** | UNIQUE (case-insensitive). ≤ 254 chars. |
| password_hash | text **NN** | Argon2id hash (TD-04). |
| is_verified, is_admin | boolean **NN** | Default `false`. |
| preferred_locale | text **NN** | Default `en`. Not CHECKed, so adding a locale needs no migration (TD-20). |
| timezone | text **NN** | IANA name, default `UTC`. |
| terms_version, privacy_version | text | Versions of the terms/privacy policy accepted at registration (`LEGAL_*_VERSION`, AUTH-002). |
| terms_accepted_at | timestamptz | |
| failed_login_count | integer **NN** | Default 0, ≥ 0. Reserved for the login lockout (M3.5), not written yet. |
| locked_until | timestamptz | Reserved for the login lockout (M3.5). |
| last_login_at | timestamptz | Set on registration and every successful login. |
| created_at, updated_at | timestamptz **NN** | |

### user_sessions
Server-side login sessions (TD-03). The cookie holds a random 256-bit token; only its SHA-256 is stored, so a database leak exposes no usable session.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid **NN** | FK `users` CASCADE. Indexed. |
| token_hash | text **NN** | UNIQUE. SHA-256 hex of the cookie token (64 chars). |
| last_seen_at | timestamptz **NN** | Last activity. The session ends after `SESSION_IDLE_TIMEOUT` (7 days) without activity. Written at most every `SESSION_TOUCH_INTERVAL` (5 min). |
| expires_at | timestamptz **NN** | Absolute end: login + 30 days with "remember me", 12 hours without. |
| user_agent | text | ≤ 512 chars. No IP address is stored. |
| created_at | timestamptz **NN** | |

A session is deleted on logout, on a login that replaces it (token rotation), and when it is found expired. A user's expired sessions are also removed at each login.

### user_profiles
One per user (`UNIQUE(user_id)`). Professional data only. Sensitive attributes (age, gender, nationality, religion, health, marital status, photo) are deliberately absent (MATCH-004).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid **NN** | FK `users` CASCADE, UNIQUE. |
| display_name | text | |
| desired_roles | jsonb **NN** | `["python developer", …]`. |
| country | varchar(2) | ISO 3166-1 alpha-2. |
| region, city | text | City level only. Coordinates and IPs are never stored (LOCATION-004, TD-15). |
| location_source | text | `manual \| ip \| browser`. |
| salary_min, salary_max | numeric(12,2) | ≥ 0, `min ≤ max`. |
| salary_currency | varchar(3) | ISO 4217. |
| salary_period | text | `hour \| day \| month \| year \| unknown`. |
| work_formats | jsonb **NN** | Values of `remote \| hybrid \| onsite`. |
| employment_types | jsonb **NN** | Values of `full_time \| part_time \| contract \| internship \| temporary`. |
| experience_level | text | `intern \| junior \| middle \| senior \| lead`. |
| languages | jsonb **NN** | `[{"code": "en", "level": "C1"}, …]`. |
| relocation_ready | boolean **NN** | Default `false`. |
| summary | text | ≤ 5000 chars. Free-text professional summary (stands in for a CV in the MVP). |
| ai_use_profile | boolean **NN** | Default `true`. Consent to use the profile in AI context (AMB-09). |
| ai_use_history | boolean **NN** | Default `false`. Consent to use search history in AI context. |
| created_at, updated_at | timestamptz **NN** | |

### skills
Curated skill dictionary (GAP-14), seeded later.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | text **NN** | Display name. |
| normalized_name | text **NN** | UNIQUE. |
| created_at | timestamptz **NN** | |

### skill_aliases
Synonyms for skill extraction and matching (e.g. `js` → JavaScript).

| Column | Type | Notes |
|---|---|---|
| alias_normalized | text PK | One alias maps to exactly one skill. |
| skill_id | uuid **NN** | FK `skills` CASCADE. Indexed. |
| locale | text | Optional locale of the alias. |
| created_at | timestamptz **NN** | |

### user_skills
PK `(user_id, skill_id)`.

| Column | Type | Notes |
|---|---|---|
| user_id | uuid **NN** | FK `users` CASCADE. |
| skill_id | uuid **NN** | FK `skills` RESTRICT. Indexed. |
| skill_level | text | `beginner \| intermediate \| advanced \| expert`. |
| years_experience | numeric(3,1) | 0–70. |
| created_at, updated_at | timestamptz **NN** | |

### sources
Job source registry (TD-08, TD-23). Sources are disabled, never deleted.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| adapter_key | text **NN** | UNIQUE. Maps to the adapter class in the registry. |
| name, base_url | text **NN** | |
| source_type | text **NN** | `api \| rss \| ats`. |
| is_enabled | boolean **NN** | Default `false`. **CHECK: can be `true` only when `compliance_reviewed_at` is set.** |
| terms_url | text | |
| rate_limit | jsonb **NN** | Token-bucket settings for the adapter. |
| storage_policy | text **NN** | `full \| snippet \| none`, default `none` (the most restrictive). |
| retention_days | integer | > 0. |
| attribution_text | text | Required attribution shown in the UI (GAP-24). |
| compliance_reviewed_at | timestamptz | Set when the SRS §17 review is done. |
| compliance_notes | text | |
| created_at, updated_at | timestamptz **NN** | |

### vacancies
One job posting from one source. Cross-source duplicates are grouped, never merged (TD-16).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| source_id | uuid **NN** | FK `sources` RESTRICT. |
| external_id | text | The source's ID. **UNIQUE `(source_id, external_id)` where not null** (dedup step 1). |
| url, canonical_url | text **NN** | CHECK `^https?://`. `canonical_url` is indexed (dedup step 2). |
| title, normalized_title | text **NN** | `normalized_title` has a GIN trigram index (dedup step 4). |
| company | text | As published by the source. There is no companies table. |
| company_normalized | text | Normalized name for fuzzy dedup. |
| description | text | Stored only when the source's `storage_policy` is `full`. |
| description_snippet | text | ≤ 500 chars (`snippet` policy). |
| requirements, responsibilities, skills | jsonb **NN** | Lists. `skills` has a GIN index. |
| language | text | ISO 639-1 language of the posting. |
| salary_min, salary_max | numeric(12,2) | As published. ≥ 0, `min ≤ max`. |
| salary_currency | varchar(3) | ISO 4217. |
| salary_period | text | `hour \| day \| month \| year \| unknown`. |
| salary_raw | text | Original salary text. |
| salary_monthly_min_eur, salary_monthly_max_eur | numeric(12,2) | Approximate EUR/month for filtering and matching only (TD-14). `min ≤ max`. |
| country | varchar(2) | ISO 3166-1 alpha-2. |
| region, city, location_raw | text | |
| remote_type | text | `remote \| hybrid \| onsite`. |
| employment_type | text | `full_time \| part_time \| contract \| internship \| temporary`. |
| experience_level | text | `intern \| junior \| middle \| senior \| lead`. |
| published_at | timestamptz | |
| collected_at | timestamptz **NN** | Default `now()`. |
| last_verified_at | timestamptz | |
| content_hash | text **NN** | sha256 hex (64 chars) of the AMB-24 fields. Indexed (dedup step 3). |
| duplicate_group_id | uuid | Same value for cross-source duplicates. Indexed. |
| is_active | boolean **NN** | Default `true`. **CHECK `is_active = (removed_at IS NULL)`.** |
| removed_at | timestamptz | When the source reported the posting closed (GAP-21). |
| created_at, updated_at | timestamptz **NN** | |

### searches
A set of search parameters (`SearchParamsV1`, TD-10). Each run is a `search_jobs` row.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid **NN** | FK `users` CASCADE. |
| title | text | Display title. |
| original_text | text | Natural-language request (≤ 2000 chars). `NULL` for form searches. |
| normalized_params | jsonb **NN** | `SearchParamsV1`. |
| params_schema_version | smallint **NN** | Default 1, > 0. |
| channel | text **NN** | `form \| ai`. |
| status | text **NN** | `draft \| confirmed \| archived`, default `draft`. |
| created_at, updated_at | timestamptz **NN** | Index `(user_id, created_at DESC)`. |

### search_jobs
One asynchronous run of a search (SEARCH-006). The transition rules live in the backend job service (architecture §6.2), not the database.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| search_id | uuid **NN** | FK `searches` CASCADE. Index `(search_id, created_at DESC)`. |
| user_id | uuid **NN** | FK `users` CASCADE. Denormalized for user-scoped queries and limits. |
| status | text **NN** | `pending \| running \| completed \| partially_completed \| failed \| cancelled`, default `pending`. |
| idempotency_key | text | **UNIQUE `(user_id, idempotency_key)`** (TD-12). |
| request_id | text | Correlation ID (TD-21). |
| context | jsonb **NN** | Params + permitted profile snapshot (architecture §6.3). |
| progress | jsonb **NN** | `{sources_total, sources_done, results_so_far}` (TD-25). |
| deadline_at | timestamptz **NN** | `created_at + 120 s`. |
| cancel_requested_at, started_at, finished_at | timestamptz | CHECK `finished_at ≥ started_at`. |
| error_code, error_message | text | |
| result_count | integer **NN** | Default 0, ≥ 0. |
| created_at, updated_at | timestamptz **NN** | |

Partial indexes `WHERE status IN ('pending','running')`: `ix_search_jobs_active_user_id` (max 2 active jobs per user) and `ix_search_jobs_active_deadline_at` (stuck-job sweeper).

### search_job_sources
Per-source outcome of a job, used to decide `completed` vs `partially_completed`. PK `(search_job_id, source_id)`.

| Column | Type | Notes |
|---|---|---|
| search_job_id | uuid **NN** | FK `search_jobs` CASCADE. |
| source_id | uuid **NN** | FK `sources` RESTRICT. Indexed. |
| status | text **NN** | `pending \| running \| succeeded \| failed \| timed_out \| skipped`, default `pending`. |
| fetched_count, stored_count | integer **NN** | Default 0, ≥ 0. |
| error_code | text | |
| duration_ms | integer | ≥ 0. |
| created_at, updated_at | timestamptz **NN** | |

### search_job_results
Vacancies returned by a job, at most 200 per job (TD-13). PK `(search_job_id, vacancy_id)`, which also makes ingest idempotent.

| Column | Type | Notes |
|---|---|---|
| search_job_id | uuid **NN** | FK `search_jobs` CASCADE. Index `(search_job_id, rank)`. |
| vacancy_id | uuid **NN** | FK `vacancies` CASCADE. Indexed. |
| rank | integer **NN** | > 0. |
| match_score | numeric(5,4) | 0–1. Internal sort key, never shown to users (MATCH-003). |
| created_at | timestamptz **NN** | |

### vacancy_matches
Deterministic match result per job and vacancy (TD-11). **UNIQUE `(search_job_id, vacancy_id)`**.

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid **NN** | FK `users` CASCADE. Index `(user_id, vacancy_id, created_at DESC)` finds the latest match for compare (AMB-17). |
| search_job_id | uuid **NN** | FK `search_jobs` CASCADE. |
| vacancy_id | uuid **NN** | FK `vacancies` CASCADE. Indexed. |
| match_level | text **NN** | `high \| medium \| low`. |
| match_score | numeric(5,4) | 0–1, internal only. |
| factors | jsonb **NN** | `[{name, score, status, detail}]`. |
| matching_skills, missing_skills | jsonb **NN** | Lists. |
| explanation | text | Prose derived only from `factors`. |
| explanation_locale | text | |
| explanation_source | text | `template \| ai`. |
| algorithm_version | smallint **NN** | > 0. |
| created_at | timestamptz **NN** | |

### saved_vacancies
**UNIQUE `(user_id, vacancy_id)`** (SAVED-003).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid **NN** | FK `users` CASCADE. Index `(user_id, created_at DESC)`. |
| vacancy_id | uuid **NN** | FK `vacancies` **RESTRICT**: retention can never delete a saved vacancy. Indexed. |
| note | text | ≤ 2000 chars. |
| created_at, updated_at | timestamptz **NN** | |

### search_history_events

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid **NN** | FK `users` CASCADE. Index `(user_id, created_at DESC)`. |
| search_id | uuid | FK `searches` CASCADE. Indexed. |
| event_type | text **NN** | `search_created \| search_confirmed \| search_run`. |
| metadata | jsonb **NN** | Small, non-sensitive event details. The ORM attribute is `event_metadata`, because `metadata` is reserved by SQLAlchemy. |
| created_at | timestamptz **NN** | |

### ai_interactions
Metadata of AI calls for cost, quota and quality tracking. **Prompt and response content is not stored** (SRS §7.12, CLAUDE.md §7).

| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid **NN** | FK `users` CASCADE. Index `(user_id, created_at DESC)` for daily quotas. |
| search_id | uuid | FK `searches` **SET NULL**: usage records outlive a deleted search. Indexed. |
| task | text **NN** | `parse_query \| clarify \| explain_match`. |
| provider, model, prompt_version | text **NN** | |
| request_id | text | |
| status | text **NN** | `succeeded \| failed`. |
| error_code | text | |
| input_tokens, output_tokens, latency_ms | integer | ≥ 0. |
| cost_estimate_usd | numeric(10,6) | ≥ 0. |
| created_at | timestamptz **NN** | |

## Deletion semantics

| Action | Effect |
|---|---|
| Delete a user (AMB-14) | `DELETE FROM users` cascades to every user-owned table: sessions, profile, skills, searches, jobs, job sources, results, matches, saved vacancies, history events and AI interactions. Shared `vacancies`, `sources` and `skills` stay. |
| Delete one search | Cascades to its jobs → job sources, results, matches. Its history events are deleted. AI interactions keep their metrics with `search_id = NULL`. |
| Delete history (AMB-13) | The service deletes the user's searches (cascades as above), remaining history events and AI interactions. Saved vacancies are kept. |
| Vacancy retention (TD-24) | Deleting a vacancy cascades to job results and matches. It is **blocked** while any user has it saved. |
| Sources | Can't be deleted while vacancies or job runs reference them. Disable them instead. |

All of these are covered by `backend/tests/integration/db/test_cascades.py`.

## Scope decisions

The initial schema covers the SRS §1.3 MVP scope. Entities that were considered:

| Entity | Decision |
|---|---|
| Users | `users` + `user_profiles`. |
| Skills | `skills`, `skill_aliases`, `user_skills`. |
| Jobs (postings) | `vacancies`. The SRS and API call them vacancies. "Job" in this codebase means an asynchronous search run (`search_jobs`). |
| Job sources | `sources`. |
| Searches | `searches`, `search_jobs`, `search_job_sources`, `search_job_results`, `search_history_events`. |
| Job matches | `vacancy_matches`. |
| Saved jobs | `saved_vacancies`. |
| AI analyses | `ai_interactions` (call metadata) + `vacancy_matches.explanation` (the analysis output). No prompt/response content is stored. |
| Companies | **No table.** Sources provide free-text company names with no stable IDs. A companies table would need entity resolution, and no requirement needs company-level data. `vacancies.company` + `company_normalized` cover display, compare and dedup. |
| Resumes | **No table.** A CV builder is out of MVP scope (SRS §1.3) and no CV upload is specified. The professional data a resume would hold lives in `user_profiles` (summary, roles, experience, languages) and `user_skills`. |
| Applications | **No table.** Auto-apply is out of MVP scope (SRS §1.3), and application tracking isn't specified. |
| Notifications | **No table.** Explicitly "Won't (MVP)" (GAP-20). |

Tables from `architecture-plan.md` §7.2 that are **deferred** to the milestone that uses them:

| Table | Milestone |
|---|---|
| `auth_events` | M3.5 rate limiting and lockout |
| `password_reset_tokens` | M3.6 password reset |
| `email_verification_tokens` | M3.7 email verification |
| `fx_rates` | M5 salary normalization (TD-14) |
| `audit_log` | M11 account deletion |

## Migrations

```bash
cd backend
uv run alembic upgrade head                               # apply
uv run alembic revision --autogenerate -m "<message>"     # then review by hand
uv run alembic check                                      # fails if models and migrations differ
uv run alembic upgrade head --sql                         # offline SQL for review
```

- Autogenerate does not detect changes to CHECK constraints or extensions. Write those by hand.
- `alembic/env.py` renders `StrEnumType` columns as `sa.Text()`, so migrations never import app code.
- Never edit an applied migration. Add a new one.

## Tests

| Test | What it covers |
|---|---|
| `tests/unit/test_model_metadata.py` | No DB: expected table set, every FK indexed, user FKs cascade, every enum column has a CHECK, all timestamps are timezone-aware. |
| `tests/unit/test_ids.py` | UUIDv7 version, variant, timestamp and ordering. |
| `tests/integration/db/test_migrations.py` | Downgrade to base and upgrade again, models ↔ migrations drift, extensions installed. |
| `tests/integration/db/test_constraints.py` | Defaults, `updated_at`, case-insensitive email, enum and range CHECKs, the compliance gate, partial unique dedup key, idempotency key, saved-once rule. |
| `tests/integration/db/test_cascades.py` | The deletion semantics above. |

Database tests need `TEST_DATABASE_URL` (see `backend/.env.example`) and are skipped without it. The test database is **dropped and rebuilt from migrations** at the start of each run. Each test runs in a transaction that is rolled back.
