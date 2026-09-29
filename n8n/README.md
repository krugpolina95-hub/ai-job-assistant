# n8n

Self-hosted n8n (pinned in `docker-compose.yml`) with its own Postgres database (`n8n-db`).

n8n is a thin orchestrator (TD-06): it only calls the backend through `/internal/v1/*`, has no access to the application database, and never calls AI providers or job sources directly.

- `workflows/`: exported workflow JSON, named `WF-<nnn> <Name>`. **No credentials or secrets** in exported files. No workflows exist yet: WF-001/WF-002 and the sweep Cron are roadmap M6.3–M6.5. The backend side they call is ready (M6.1–M6.2).
- Editor (local): http://127.0.0.1:5678. Create the owner account on first start.
- `N8N_ENCRYPTION_KEY` in the root `.env` encrypts stored credentials. Keep it stable, because changing it makes existing credentials unreadable.

```bash
docker compose up -d n8n
```

## Backend contract (for the workflows)

Full reference: `docs/api.md` → "Internal API". Summary:

**Secrets.** Three different values, each in the root `.env` for the backend and, as n8n credentials, for the workflows: `N8N_WEBHOOK_SECRET` (verifies the trigger), `INTERNAL_API_TOKEN` and `INTERNAL_HMAC_SECRET` (sign calls to the backend). Never put them into workflow JSON.

**Signature (both directions).** `X-Timestamp`: Unix seconds. `X-Signature`: `sha256=` + hex HMAC-SHA256 of the four lines `timestamp`, `METHOD`, `path` (with query), `sha256 hex of the raw body` joined with `\n`. Requests older or newer than 300 s are refused.

**WF-001 Search Request** (webhook `POST /webhook/wf-001-search`, body `{job_id, request_id}`):
1. Code node: verify the trigger signature with `N8N_WEBHOOK_SECRET` (path `/webhook/wf-001-search`); stop if invalid.
2. `GET /internal/v1/jobs/{job_id}/context` → `{status, deadline_at, sources[]}`.
3. `POST /internal/v1/jobs/{job_id}/status` `{"status": "running"}`; `410` means the run is over: stop.
4. For each source key, in parallel, WF-002.
5. `POST /internal/v1/jobs/{job_id}/finalize`.
6. Error Trigger path: nothing to undo; the backend's deadline and the sweep finish the run.

**WF-002 Source Search:** `POST /internal/v1/jobs/{job_id}/sources/{key}/run` with a timeout above the run deadline (the backend bounds the work by it; default 120 s). Retry once on 5xx or a timeout: the call is idempotent. `410`: stop. The response is the source's outcome.

**Cron sweep:** `POST /internal/v1/maintenance/sweep-jobs` every minute.

Every call authenticates with `Authorization: Bearer <INTERNAL_API_TOKEN>` plus the signature above.
