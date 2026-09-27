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
| Account enumeration | Login returns the same `401 INVALID_CREDENTIALS` for an unknown email and a wrong password, and runs a dummy Argon2 verification for unknown emails so both take the same time. Registration does reveal an existing email (`409`), a product decision in `requirements-analysis.md` F1. |
| Consent | Registration requires `accept_terms: true` and stores the accepted `LEGAL_TERMS_VERSION`/`LEGAL_PRIVACY_VERSION` and time (AUTH-002, GAP-05). |
| Secure cookies in deployments | Startup fails if `SESSION_COOKIE_SECURE=false` in `staging` or `production`. |

Protected routes use the `CurrentAuth` dependency; unsafe ones use `CsrfProtectedAuth`. `backend/tests/authz/test_route_protection.py` discovers every `/api/v1` route and fails if a non-public route answers without a session, doesn't depend on authentication, or is unsafe without the CSRF check.

## Request handling

- Request bodies are capped at 64 KB (`413`). Unknown JSON fields are rejected.
- Error responses never echo submitted values, and unexpected errors return a generic `500` (details are only in logs).
- Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.

## Logging and secrets

- Logs are JSON. User IDs are logged only as a keyed hash (`LOG_HASH_SECRET`, HMAC-SHA256). Emails, passwords, tokens and cookies are never logged, and keys like `password`, `token`, `email`, `cookie` are masked by a last-resort redaction processor.
- Query strings are not logged (they may carry personal data). No IP address is stored in sessions.
- All secrets come from environment variables (`backend/app/core/config.py`), and required ones have no defaults. Opaque session tokens need no signing key.

## Not implemented yet

| Gap | Planned in |
|---|---|
| Rate limiting and progressive lockout on login/register (brute-force protection, AUTH-006); `auth_events` audit log of suspicious logins | M3.5 (needs the Redis limiter, M1.6) |
| Password reset and change password; revoke other sessions on change | M3.6 |
| Email verification | M3.7 |
| "Log out everywhere" and a session list | Later M3 task |
| Cleanup of expired sessions of inactive users (they are removed on access and at the user's next login) | Retention job (M11/M12) |
