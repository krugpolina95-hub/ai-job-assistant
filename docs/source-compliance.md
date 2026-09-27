# Source Compliance

SRS §17 requires a technical and legal check of every job source before it is used. Each source has a section below that answers the §17 checklist. A source may only be enabled after a person has completed its review:

```bash
cd backend
uv run python -m app.cli sources sync                                   # register sources from code (disabled)
uv run python -m app.cli sources enable <key> --confirm-compliance-review  # records the review date
uv run python -m app.cli sources disable <key>                          # stops it for new runs immediately
```

The code enforces this: a source runs only if `sources.is_enabled` is true, and the database refuses `is_enabled` without `compliance_reviewed_at` (`ck_sources_enabled_requires_review`). No code path sets `compliance_reviewed_at` automatically.

## Rules for every source (SOURCE-003, CLAUDE.md §7)

- Official API or explicitly permitted feed only. No scraping, no CAPTCHA or login bypass, no rate-limit evasion.
- Honest identity: `User-Agent: AIJobAssistant/<version> (+<SOURCES_CONTACT>)`. Never imitate a browser.
- Outbound requests go only to the host fixed in the adapter. URLs from users or from source data are never fetched.
- Store only what the storage policy allows (TD-23). Keep the original link and show the attribution the source requires.

## Adding a source

1. Answer the checklist in a new section of this file, and keep the source disabled until the answers are complete.
2. Add `app/integrations/sources/<key>/adapter.py` implementing `SourceAdapter`. It maps the source's format to `VacancyIn` and raises `SourceError` subclasses on failure.
3. Add one `SourceDefinition` in `app/integrations/sources/registry.py` (name, host, storage policy, attribution, rate limit).
4. Add recorded or documented-format fixtures and respx tests (no live calls).
5. `sources sync`, then `sources enable <key> --confirm-compliance-review` once the review is signed off.

Ingestion, deduplication, storage and the vacancy API need no changes.

---

## Adzuna (`adzuna`)

**Status: DRAFT, NOT REVIEWED. Registered disabled.** The adapter is implemented and tested against mocked responses only. Open items below must be resolved by the product owner (and legal, for commercial use) before enabling.

| §17 check | Answer | Evidence |
|---|---|---|
| Official API | Yes. REST/JSON job search API: `GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}`. | [API docs](https://developer.adzuna.com/docs/search) |
| Availability for our type of application | Registration gives an `app_id` and `app_key`. Commercial use is allowed with attribution, **but the terms say trial users need written consent for ongoing commercial applications.** | [Overview](https://developer.adzuna.com/overview), [Terms](https://developer.adzuna.com/docs/terms_of_service) |
| Terms of use | Prohibits contacting third-party content providers, extracting confidential information for commercial reuse, multiple accounts for one entity, and aggregated use of trial data without written consent. Either party may terminate at any time; all data must then be removed. | Terms |
| Rate limits | Default: 25 requests/minute, 250/day, 1000/week, 2500/month. One run makes at most 2 requests (≤ 100 results), so the default quota supports about 125 runs a day. | Terms |
| Storage and display | Each listing must be labelled "Jobs by Adzuna" (at least 116×23 px, "Jobs" linking to Adzuna and the Adzuna logo linked). The API returns only a description **snippet**. | Terms, API docs |
| OAuth / app verification | No OAuth. App id and key sent as query parameters (so the backend keeps them out of logs, see `docs/security.md`). | API docs |
| Caching | Not addressed in the terms. **Open question.** | Terms |
| Link to original | `redirect_url` is the only link provided; it points to Adzuna, which forwards to the posting. We store and show it unchanged. | API docs |

**Configuration in code** (`registry.py`): `storage_policy = snippet` (we never store more than the API returns); `attribution_text = "Jobs by Adzuna"`; `retention_days = 30` (a proposal, since the terms set no caching period); `rate_limit = 25/min, 250/day`.

**Data rules implemented in the adapter:**
- Salaries flagged `salary_is_predicted = 1` are Adzuna estimates and are dropped, because we never show guessed salaries (VACANCY-003).
- Salaries are treated as annual amounts in the country's currency.
- Links that don't point to an Adzuna domain are rejected.
- Remote type and experience level are not provided and stay empty.

**Coverage:** 19 countries (GB, US, AT, AU, BE, BR, CA, CH, DE, ES, FR, IN, IT, MX, NL, NZ, PL, SG, ZA). **No Lithuania or Belarus** (RISK-01). Searches for other countries return no results from this source, without a request.

**Open items before enabling:**
1. Written consent from Adzuna for commercial use (the product is a SaaS), or confirmation that the account is not a trial account.
2. Caching and retention: confirm with Adzuna how long results may be stored; adjust `retention_days`.
3. Attribution rendering in the UI (logo, size, links): frontend task M8.3.
4. Confirm the annual-salary assumption with a recorded response once credentials exist, then replace the documented-format fixture with a recorded one.
5. Per-source rate limiting in Redis (M5.6): until then, respect the quota operationally (runs are manual via the CLI).

## Mock source (`mock`)

Offline fixtures shipped with the code (`app/integrations/sources/mock/`). No external party, no network, no personal data. For development, tests and demos only; not to be enabled in production.

## Considered and not used yet

| Source | Why not now |
|---|---|
| LinkedIn, Indeed | Partner-gated APIs; scraping prohibited (SRS §17). |
| hh.ru / rabota.by (HeadHunter API) | Best coverage for the Belarus audience, but its current terms and whether vacancy search needs an application token could not be verified, and using a Russian company's API needs a jurisdiction review (RISK-11). Candidate for source #2 after that review. |
| Search engines | Return links, not structured vacancies; terms usually restrict this use (AMB-19). |
