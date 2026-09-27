# database

PostgreSQL 16 configuration for the application database.

- **Schema:** managed only by Alembic migrations in `backend/alembic/`. Nothing in this folder creates tables, and no business logic lives in the database (triggers/procedures are not used; constraints and indexes are).
- **`init/`:** mounted into the `postgres` container at `/docker-entrypoint-initdb.d`. The scripts run **once**, when the `postgres-data` volume is first created.
  - `01-create-test-database.sh` creates `<POSTGRES_DB>_test` for backend integration tests.
- **Extensions** (`citext`, `pg_trgm`) are created by the initial Alembic migration, so every database built from migrations gets them.
- The schema itself is documented in `docs/database.md`.
- **n8n** uses its own `n8n-db` container. It has no access to this database (TD-06).

## Common tasks

```bash
docker compose up -d postgres                         # start the database
docker compose exec postgres psql -U app ai_job_assistant   # open a psql shell (use your POSTGRES_USER/POSTGRES_DB)
docker compose down && docker volume rm ai-job-assistant_postgres-data   # reset: re-runs init scripts on next start
```
