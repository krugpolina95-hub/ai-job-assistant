# AI Job Assistant

Web app that turns a natural-language job request into structured search parameters, collects vacancies from reviewed sources, and explains how each one matches the user's profile.

> **Status:** project foundation, database schema, backend foundation and backend authentication (register, login, logout, current user, password change and email reset, rate limits, account lockout and a security event log) the vacancy module (source adapter port, mock source, ingestion with deduplication, list and details API) the first real source adapter (Adzuna, disabled until its compliance review is complete) and asynchronous search runs (inline dispatcher, per-source rate limits, partial failure, retries, filter reporting). n8n orchestration, matching, AI and the frontend pages are not implemented yet.

## Architecture

```
Browser → Caddy → Next.js ──/api/v1 rewrite──▶ FastAPI ──▶ PostgreSQL
                                                  ├──▶ Redis (rate limits, caches)
                                          n8n ◀───┘  (own DB, /internal/v1 only)
```

The browser talks only to the Next.js origin. FastAPI is not published (TD-05). See `docs/architecture-plan.md` and `docs/technical-decisions.md`.

| Path | Contents |
|---|---|
| `frontend/` | Next.js (App Router, TypeScript strict), Tailwind CSS with design tokens, next-intl (EN/RU) |
| `backend/` | FastAPI, Pydantic settings, async SQLAlchemy models + Alembic migrations (schema: `docs/database.md`), health/readiness endpoints |
| `database/` | PostgreSQL init scripts (see `database/README.md`) |
| `n8n/` | Exported workflows (none yet) and setup notes |
| `docs/` | SRS, requirements analysis, architecture, technical decisions, roadmap, API reference (`api.md`), database schema (`database.md`), security controls (`security.md`), job source reviews (`source-compliance.md`) |
| `docker-compose.yml`, `Caddyfile` | Local stack |

## Prerequisites

- Docker with Compose v2
- Python 3.12 via [uv](https://docs.astral.sh/uv/) (`pip install uv` or the standalone installer; uv downloads Python 3.12 if needed)
- Node.js 22 LTS and pnpm 12 (`corepack enable`, or `npm install -g pnpm@12.6.0`)

## Quick start (full stack in Docker)

```bash
cp .env.example .env              # then replace every change-me value
docker compose up -d --build
```

| URL | Service |
|---|---|
| http://localhost | App (Caddy → Next.js) |
| http://localhost/api/v1/health | Backend liveness, through the Next.js rewrite |
| http://localhost/api/v1/ready | Backend readiness (database + Redis) |
| http://127.0.0.1:8025 | Mailpit (captured email) |
| http://127.0.0.1:5678 | n8n editor |

## Local development (apps on the host, infrastructure in Docker)

```bash
cp .env.example .env
docker compose up -d postgres redis mailpit

# backend
cd backend
cp .env.example .env              # set the password to match POSTGRES_PASSWORD
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload     # http://localhost:8000/api/v1/docs

# frontend (another terminal)
cd frontend
cp .env.example .env.local
pnpm install
pnpm dev                          # http://localhost:3000
```

## Checks

```bash
# backend
cd backend
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest                     # DB tests run only when TEST_DATABASE_URL is set (backend/.env)
uv run alembic check              # models and migrations are in sync

# frontend
cd frontend
pnpm lint && pnpm typecheck && pnpm test && pnpm build   # build needs BACKEND_INTERNAL_URL

# docker
docker compose config --quiet
```

## Job sources

Sources are registered from code and stay disabled until a person completes their compliance review (`docs/source-compliance.md`). Until n8n orchestration exists, a source can be run by hand:

```bash
cd backend
uv run python -m app.cli sources sync
uv run python -m app.cli sources list
uv run python -m app.cli sources enable mock --confirm-compliance-review   # offline fixtures, dev only
uv run python -m app.cli sources run mock --country LT
```

Adzuna additionally needs `SOURCES_CONTACT`, `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` (see `backend/.env.example`). The CLI prints JSON on stdout and logs on stderr.

## Configuration

All settings come from environment variables. Each `.env.example` lists every variable with a placeholder and a description. Real `.env` files are git-ignored.

| File | Used by |
|---|---|
| `.env.example` → `.env` | Docker Compose (database credentials, n8n, Caddy) |
| `backend/.env.example` → `backend/.env` | Backend run on the host (`app/core/config.py`) |
| `frontend/.env.example` → `frontend/.env.local` | Frontend run on the host (`lib/env.ts`) |

Required variables have no defaults. The backend, the frontend build and `docker compose` all stop with an error when one is missing.

## Contributing

Engineering rules are in `CLAUDE.md`. Work on short-lived branches with Conventional Commits.
