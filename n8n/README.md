# n8n

Self-hosted n8n (pinned in `docker-compose.yml`) with its own Postgres database (`n8n-db`).

n8n is a thin orchestrator (TD-06): it only calls the backend through `/internal/v1/*`, has no access to the application database, and never calls AI providers or job sources directly.

- `workflows/`: exported workflow JSON, named `WF-<nnn> <Name>`. **No credentials or secrets** in exported files. No workflows exist yet (roadmap M6).
- Editor (local): http://127.0.0.1:5678. Create the owner account on first start.
- `N8N_ENCRYPTION_KEY` in the root `.env` encrypts stored credentials. Keep it stable, because changing it makes existing credentials unreadable.

```bash
docker compose up -d n8n
```
