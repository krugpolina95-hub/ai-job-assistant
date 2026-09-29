# Security

How the implemented security controls work. Decisions are in `technical-decisions.md` (TD-03, TD-04, TD-05, TD-21), the overall threat controls in `architecture-plan.md` §13. This file describes what the code does today and what is still missing.

## Authentication (M3)

| Control | Implementation |
|---|---|
| Password storage | Argon2id via pwdlib, `m=19 MiB, t=2, p=1` by default (`PASSWORD_HASH_*`, TD-04). Only the hash is stored (`users.password_hash`). Hashes with outdated parameters are replaced at the next successful login. Hashing runs in a worker thread so it doesn't block the event loop. |
| Password rules | 10–128 characters, used exactly as typed (no trimming), no composition rules, must not equal the email (TD-04). |
| Sessions | Random 256-bit token in the `sid` cookie (`HttpOnly; Secure; SameSite=Lax`). The database stores only its SHA-256 (`user_sessions.token_hash`), so a leaked database gives no usable sessions. Absolute expiry 12 h (browser session) or 30 days ("remember me"), idle expiry 7 days (TD-03). No JWT and nothing in `localStorage`. |
| Session fixation | Every login issues a new token and deletes the session previously held by that browser. |
| Logout | Deletes the current session row, so the token stops working immediately, and clears both cookies. Other devices stay signed in. |
| CSRF | Double-submit: the readable `csrf` cookie must be echoed in `X-CSRF-Token` on every unsafe request to a protected route, compared in constant time. Together with `SameSite=Lax` and JSON-only bodies (a cross-site HTML form can't produce `application/json`), this covers login and logout CSRF. |
| Account enumeration | Login returns the same `401 INVALID_CREDENTIALS` for an unknown email, a wrong password and a locked account, and runs a dummy Argon2 verification for unknown emails so all take the same time. `forgot-password` always answers `202` with an empty body and sends the email in the background, so neither the answer nor its timing depends on the account existing. Registration does reveal an existing email (`409`), a product decision in `requirements-analysis.md` F1, bounded by its rate limit. |
| Request limits (AUTH-006) | Sliding-window limits in Redis per client IP, email or user (`app/services/auth_guard.py`, table in `docs/api.md`): failed logins 5/min per IP+email and 20/hour per IP, registrations 5/hour per IP, reset requests 5/hour per IP and 3/hour per email, reset attempts 10/hour per IP, password changes 5 per 15 min per user. A hit answers `429 RATE_LIMITED` with `Retry-After`. Redis keys are HMAC hashes, never a raw email or IP. If Redis is unreachable the limits let requests through (logged as `rate_limiter_unavailable`): Redis is a cache (TD-18), and the lockout below still protects accounts. |
| Account lockout | `LOGIN_LOCKOUT_THRESHOLD` (10) failed logins in a row lock the account for `LOGIN_LOCKOUT_DURATION` (15 min), counted with one atomic `UPDATE` so parallel attempts can't slip past. It lives in the database, independent of Redis. During the lock even the right password gets the generic `401`; a successful login resets the count and a password reset lifts the lock. |
| Client IP | Taken from `X-Forwarded-For` only as far as the proxies are trusted: with `TRUSTED_PROXY_COUNT=N`, the N-th entry from the right (the one the outermost trusted proxy wrote), otherwise the TCP peer. Entries a client adds itself are ignored. Compose sets 1 (Caddy appends, the Next.js rewrite keeps the header). |
| Security events | `auth_events` records logins, failures (with reason), lockouts, rate-limit hits, password changes and reset requests/resets, with the email as a keyed hash, the IP as its `/24` or `/48` network and the User-Agent. No raw email, IP, password or token. |
| Password change | Requires the session, CSRF and the current password (wrong → `400 INVALID_CURRENT_PASSWORD`, logged as a failed login). All the user's sessions end, this browser's included, and the browser gets a fresh session, so a copied cookie stops working (TD-03). |
| Password reset | `forgot-password` emails a link with a 256-bit single-use token valid for 30 min (`PASSWORD_RESET_TOKEN_TTL`); only its SHA-256 is stored, and only the newest link of an account works. The token is in the URL fragment, so it never reaches server logs or `Referer`. The reset claims the token with one conditional `UPDATE` (works once, even concurrently), sets the password, deletes every session and lifts a lockout. Unknown, expired and used tokens get the same `400 RESET_TOKEN_INVALID`. |
| Email delivery | SMTP (`SMTP_*`, TD-27): Mailpit locally; staging/production refuse to start without a real host, `SMTP_SECURITY` `starttls`/`tls`, a sender on a real domain and an https `APP_PUBLIC_URL`. Delivery failures are logged without the address. |
| Consent | Registration requires `accept_terms: true` and stores the accepted `LEGAL_TERMS_VERSION`/`LEGAL_PRIVACY_VERSION` and time (AUTH-002, GAP-05). |
| Secure cookies in deployments | Startup fails if `SESSION_COOKIE_SECURE=false` in `staging` or `production`. |

Protected routes use the `CurrentAuth` dependency; unsafe ones use `CsrfProtectedAuth`. `backend/tests/authz/test_route_protection.py` discovers every `/api/v1` route and fails if a non-public route answers without a session, doesn't depend on authentication, or is unsafe without the CSRF check.

## Request handling

- Request bodies are capped at 64 KB (`413`). Unknown JSON fields are rejected, which also keeps sensitive attributes (age, gender, nationality, religion, health, marital status, photo) out of profiles.
- Profile and settings routes act only on the caller's own rows (`user_id` from the session, never from the request; a `user_id` in the body is rejected as an unknown field), so no request can read or change another user's profile, skills, settings or consents.
- Consents (`ai_use_profile`, `ai_use_history`, PROFILE-003) can be changed only through `PATCH /settings`, never as a side effect of a profile update or deletion. Each change is logged (`settings_updated`, field names and boolean values only). The AI context builder (M7) must read the consent from the search snapshot or the settings; nothing uses the AI yet.
- Error responses never echo submitted values, and unexpected errors return a generic `500` (details are only in logs).
- Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.

## Job sources (outbound calls)

| Control | Implementation |
|---|---|
| Compliance gate | A source runs only when enabled, and enabling requires a recorded compliance review (DB CHECK + `sources enable --confirm-compliance-review`). The gate is checked before any outbound request. See `docs/source-compliance.md`. |
| No scraping | Adapters use official APIs only. Honest `User-Agent` with a contact (`SOURCES_CONTACT`); no browser imitation. |
| SSRF | Each adapter calls one fixed host (Adzuna: `api.adzuna.com` over HTTPS). Redirects are not followed. URLs from users or from source data are never fetched; source links are only stored and shown. Adzuna links must point to an Adzuna domain or the record is rejected. |
| Credentials | From environment variables only (`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`). Adzuna takes the key as a URL parameter, so: httpx request logging is limited to warnings (it logs full URLs at INFO), httpx exceptions are never chained into our errors (they contain the URL), and source error messages never include URLs. Tests verify the key appears in neither logs nor errors. |
| Untrusted data | Source records are validated (`VacancyIn`), HTML is converted to plain text on ingest, and one bad record never fails a batch. |
| Bounded cost | At most 100 results (2 requests) per source run; request timeout `SOURCE_HTTP_TIMEOUT_SECONDS`. |
| Source rate limits | Every outbound request takes a token from the source's Redis bucket (Adzuna 25/min) and its daily quota (Adzuna 250/day), atomically across processes. Short waits are bounded by `SOURCE_RATE_LIMIT_MAX_WAIT_SECONDS` and the run deadline; a spent quota fails the source without a request; if Redis is unreachable the source fails closed (`SOURCE_RATE_LIMITER_UNAVAILABLE`). |
| User run limits | Separate from source limits: at most `SEARCH_MAX_ACTIVE_JOBS_PER_USER` active runs (enforced under a per-user row lock) and `SEARCH_MAX_RUNS_PER_HOUR` runs per hour; one active run per search (unique index); `Idempotency-Key` replays return the same run. |

## Internal API and n8n (TD-06, TD-09, M6.1–M6.2)

| Control | Implementation |
|---|---|
| Reachability | `/internal/v1` is not proxied by Next.js (only `/api/v1` is), the backend publishes no port, and n8n shares only the `automation` network with it. The routes are absent from the public OpenAPI document. |
| Authentication | Bearer service token (`INTERNAL_API_TOKEN`, constant-time compare) **and** an HMAC-SHA256 signature (`INTERNAL_HMAC_SECRET`) over timestamp, method, path with query and body hash. Signing method and path stops a captured signature from being replayed against another endpoint or run (most internal bodies are empty). Timestamps outside ±300 s are refused. The dependency sits on the router, so no internal route can skip it (checked by `tests/authz`). |
| Fail closed | Without the token and the secret configured, every internal request is `401`. `JOB_DISPATCHER=n8n` refuses to start unless the webhook URL and all three secrets are set, and the three secrets must differ (one per direction). |
| Replays within the window | Every internal step is idempotent (atomic source claims, fenced outcomes, idempotent status and finalize), so replaying a signed request inside the 300 s window changes nothing. |
| Data minimization | n8n receives run ids, statuses, source keys and counts, never search parameters, profile data or vacancy content. |
| Trigger | The backend signs the WF-001 trigger with its own secret (`N8N_WEBHOOK_SECRET`), does not follow redirects, and fails the run with `DISPATCH_ERROR` if n8n can't be reached, instead of leaving it pending. |

## AI calls (TD-07, M7.1 foundation)

No AI provider is configured yet and no endpoint calls the AI; the controls below apply to every future AI feature because they sit in the one entry point, `AIService`.

| Control | Implementation |
|---|---|
| Keys | Only the backend will call a provider; no key exists yet. When one is added it comes from backend env vars only. |
| No raw replies | Providers return raw data only to `AIClient`, which returns a validated Pydantic model or fails. Raw replies are never returned, logged, stored or put into error messages; the repair turn lists schema problems without repeating the reply (untrusted text, prompt injection). |
| Validation (AI-007) | Every reply is validated against the caller's Pydantic model; one repair attempt; then `502 AI_INVALID_RESPONSE`. |
| Bounded time and cost | 20 s timeout per call (`AI_TIMEOUT_SECONDS`), one retry on 429/5xx with jitter, none on timeouts; a circuit breaker pauses calls after 5 provider failures in a row for 60 s; per-user quotas of 10 calls/min (Redis) and 50 per 24 h (counted in `ai_interactions`). |
| Privacy | `ai_interactions` stores metadata only (task, model, tokens, latency, outcome), never prompt or reply text. |

## Logging and secrets

- Logs are JSON. User IDs are logged only as a keyed hash (`LOG_HASH_SECRET`, HMAC-SHA256). Emails, passwords, tokens and cookies are never logged, and keys like `password`, `token`, `email`, `cookie` are masked by a last-resort redaction processor.
- Query strings are not logged (they may carry personal data). No IP address is stored in sessions; `auth_events` keeps only the network prefix.
- All secrets come from environment variables (`backend/app/core/config.py`), and required ones have no defaults. Opaque session tokens need no signing key.

## Not implemented yet

| Gap | Planned in |
|---|---|
| Email verification | M3.7 |
| Rate limits for other route groups (AI, default per-user limit, architecture §4.4) | With the features (M7, M12) |
| Retention of `auth_events` (180 days) and used/expired reset tokens | Retention job (M11/M12) |
| Frontend pages for password change and reset (`/[locale]/reset-password` reads the token from the fragment) | M3.8 |
| Email provider and sender domain with SPF/DKIM/DMARC | TD-27 decision (product owner) |
| "Log out everywhere" and a session list | Later M3 task |
| Cleanup of expired sessions of inactive users (they are removed on access and at the user's next login) | Retention job (M11/M12) |
