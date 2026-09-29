"""Search-run execution with real parallel workers and committed data (M5.4-M5.9).

Adzuna is mocked with respx (no live calls) and enabled only inside this test database;
per-source rate limits are scripted; the inline dispatcher runs the real SearchJobRunner.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.sources.adzuna.adapter import BASE_URL
from app.integrations.sources.base import (
    SourceQuotaExhaustedError,
    SourceRateLimitedError,
    SourceRateLimiterUnavailableError,
)
from app.main import create_app
from app.models import SearchJob, SearchJobSource, Source, Vacancy
from app.repositories.search import SearchJobRepository
from app.services.dispatch import InlineDispatcher
from app.services.job_runner import SearchJobRunner
from app.services.source_admin_service import SourceAdminService
from tests.conftest import build_test_settings
from tests.fakes import install_test_doubles
from tests.integration.db.search_helpers import (
    RecordingDispatcher,
    ScriptedLimiter,
    create_search,
    csrf,
    run,
    sign_in,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

SETTINGS = build_test_settings(
    source_retry_delay_seconds=0,
    sources_contact="ops@example.com",
    adzuna_app_id="test-id",
    adzuna_app_key="secret-adzuna-key",
)
FIXTURE = Path(__file__).parents[2] / "fixtures" / "sources" / "adzuna" / "search_gb.json"
ADZUNA_PAGE_1 = f"{BASE_URL}/gb/search/1"
MOCK_ROLES = ["developer", "analyst", "engineer", "contractor"]
GB = {"country": "GB"}


def adzuna_payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload


@dataclass
class Harness:
    app: FastAPI
    client: AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    limiter: ScriptedLimiter
    runner: SearchJobRunner
    dispatcher: InlineDispatcher

    async def enable(self, *keys: str) -> None:
        async with self.sessions() as db:
            admin = SourceAdminService(db)
            await admin.sync_definitions()
            for key in keys:
                await admin.enable(key, compliance_review_confirmed=True)

    async def disable(self, key: str) -> None:
        async with self.sessions() as db:
            await SourceAdminService(db).disable(key)

    async def run_and_wait(self, search_id: str) -> dict[str, Any]:
        response = await run(self.client, search_id)
        assert response.status_code == 202, response.text
        await self.dispatcher.drain()
        return await self.job(response.json()["id"])

    async def job(self, job_id: str) -> dict[str, Any]:
        body: dict[str, Any] = (await self.client.get(f"/api/v1/search-jobs/{job_id}")).json()
        return body

    async def results(self, job_id: str, **params: Any) -> list[dict[str, Any]]:
        response = await self.client.get(f"/api/v1/search-jobs/{job_id}/results", params=params)
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    def use_recording_dispatcher(self) -> RecordingDispatcher:
        recording = RecordingDispatcher()
        self.app.state.dispatcher = recording
        return recording


@pytest_asyncio.fixture(loop_scope="session")
async def harness(committed_sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Harness]:
    limiter = ScriptedLimiter()
    runner = SearchJobRunner(committed_sessions, SETTINGS, limiter)
    dispatcher = InlineDispatcher(runner)
    app = create_app(SETTINGS)
    app.state.db_session_factory = committed_sessions
    app.state.dispatcher = dispatcher
    install_test_doubles(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as c:
        await sign_in(c)
        yield Harness(app, c, committed_sessions, limiter, runner, dispatcher)
    await dispatcher.drain()


def by_key(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {source["key"]: source for source in job["sources"]}


async def source_row(h: Harness, job_id: str, key: str) -> SearchJobSource:
    async with h.sessions() as db:
        row = await db.scalar(
            select(SearchJobSource)
            .join(Source)
            .where(SearchJobSource.search_job_id == uuid.UUID(job_id), Source.adapter_key == key)
        )
        assert row is not None
        return row


# --- a complete run ------------------------------------------------------------------------


async def test_run_imports_links_and_completes(harness: Harness) -> None:
    await harness.enable("mock")
    search_id = await create_search(harness.client, roles=MOCK_ROLES)

    job = await harness.run_and_wait(search_id)

    assert job["status"] == "completed"
    mock = by_key(job)["mock"]
    # The mock matches roles against titles, so the untitled record isn't returned: 7 fetched.
    assert (mock["status"], mock["attempts"], mock["fetched_count"]) == ("succeeded", 1, 7)
    # 1 invalid, and "m-1-repost" is the same vacancy as "m-1" → 5 vacancies, linked once each.
    assert job["result_count"] == mock["stored_count"] == 5
    assert job["progress"] == {"sources_total": 1, "sources_done": 1, "results_so_far": 5}
    assert mock["filters"] == {"keywords": {"mode": "source", "excluded": 0, "unverified": 0}}
    visible = await harness.results(job["id"])
    assert len(visible) == 4  # the closed posting is hidden by default
    assert len(await harness.results(job["id"], include_inactive="true")) == 5
    assert all(item["unverified_filters"] == [] for item in visible)
    listed = (await harness.client.get("/api/v1/vacancies")).json()["items"]
    assert {i["id"] for i in listed} == {i["id"] for i in visible}


async def test_results_are_isolated_between_users(harness: Harness) -> None:
    await harness.enable("mock")
    job = await harness.run_and_wait(await create_search(harness.client, roles=MOCK_ROLES))

    async with AsyncClient(
        transport=ASGITransport(app=harness.app), base_url="https://testserver"
    ) as other:
        await sign_in(other)
        assert (await other.get(f"/api/v1/search-jobs/{job['id']}/results")).status_code == 404
        assert (await other.get("/api/v1/vacancies")).json()["items"] == []
        some_vacancy = (await harness.results(job["id"]))[0]["id"]
        assert (await other.get(f"/api/v1/vacancies/{some_vacancy}")).status_code == 404


async def test_repeating_a_finished_source_run_changes_nothing(harness: Harness) -> None:
    await harness.enable("mock")
    job = await harness.run_and_wait(await create_search(harness.client, roles=MOCK_ROLES))
    row = await source_row(harness, job["id"], "mock")

    await harness.runner.run_source(uuid.UUID(job["id"]), row.source_id)

    again = await harness.job(job["id"])
    assert again["result_count"] == 5
    assert by_key(again)["mock"]["attempts"] == 1


async def test_linking_the_same_vacancy_twice_keeps_one_link(harness: Harness) -> None:
    await harness.enable("mock")
    job = await harness.run_and_wait(await create_search(harness.client, roles=MOCK_ROLES))
    vacancy_id = uuid.UUID((await harness.results(job["id"]))[0]["id"])

    async with harness.sessions() as db:
        repo = SearchJobRepository(db)
        linked = await repo.link_results(uuid.UUID(job["id"]), [(vacancy_id, [])] * 2, cap=200)
        await db.commit()
        total = await repo.count_results(uuid.UUID(job["id"]))

    assert (linked, total) == (0, 5)


# --- partial failure and retries ------------------------------------------------------------


async def test_one_failing_source_does_not_fail_the_run(harness: Harness) -> None:
    await harness.enable("mock", "adzuna")
    search_id = await create_search(harness.client, roles=MOCK_ROLES, location=GB)

    with respx.mock() as mock:
        route = mock.get(ADZUNA_PAGE_1).respond(500)
        job = await harness.run_and_wait(search_id)

    assert job["status"] == "partially_completed"
    adzuna, local = by_key(job)["adzuna"], by_key(job)["mock"]
    assert (adzuna["status"], adzuna["error_code"], adzuna["attempts"]) == (
        "failed",
        "SOURCE_UNAVAILABLE",
        2,  # retryable: tried twice
    )
    assert route.call_count == 2
    assert local["status"] == "succeeded"
    # The mock can't query by country: LT/US/DE postings are excluded, unknown ones flagged.
    assert local["filters"]["country"] == {"mode": "local", "excluded": 3, "unverified": 2}
    assert job["result_count"] == 2
    assert "secret-adzuna-key" not in json.dumps(job)


async def test_non_retryable_source_error_is_not_retried(harness: Harness) -> None:
    await harness.enable("adzuna")

    with respx.mock() as mock:
        route = mock.get(ADZUNA_PAGE_1).respond(401)
        job = await harness.run_and_wait(await create_search(harness.client, location=GB))

    adzuna = by_key(job)["adzuna"]
    assert (adzuna["error_code"], adzuna["attempts"], route.call_count) == (
        "SOURCE_AUTH_FAILED",
        1,
        1,
    )
    assert (job["status"], job["error_code"]) == ("failed", "ALL_SOURCES_FAILED")


async def test_retryable_error_then_success(harness: Harness) -> None:
    await harness.enable("adzuna")

    with respx.mock() as mock:
        mock.get(ADZUNA_PAGE_1).mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json=adzuna_payload())]
        )
        job = await harness.run_and_wait(await create_search(harness.client, location=GB))

    adzuna = by_key(job)["adzuna"]
    assert (job["status"], adzuna["status"], adzuna["attempts"]) == ("completed", "succeeded", 2)
    assert job["result_count"] == 4  # 6 results, 2 rejected by the adapter's validation


# --- per-source rate limits -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "status", "error_code", "attempts", "http_calls"),
    [
        ([SourceRateLimitedError("busy")], "succeeded", None, 2, 1),
        ([SourceQuotaExhaustedError("spent")], "failed", "SOURCE_QUOTA_EXHAUSTED", 1, 0),
        (
            [SourceRateLimiterUnavailableError("down")] * 2,
            "failed",
            "SOURCE_RATE_LIMITER_UNAVAILABLE",
            2,
            0,
        ),
    ],
    ids=["retry-after-wait", "quota-spent", "limiter-down"],
)
async def test_source_rate_limits_are_respected(
    harness: Harness,
    script: list[Exception],
    status: str,
    error_code: str | None,
    attempts: int,
    http_calls: int,
) -> None:
    await harness.enable("adzuna")
    harness.limiter.script = {"adzuna": list(script)}

    with respx.mock(assert_all_called=False) as mock:  # call counts are asserted below
        route = mock.get(ADZUNA_PAGE_1).respond(200, json=adzuna_payload())
        job = await harness.run_and_wait(await create_search(harness.client, location=GB))

    adzuna = by_key(job)["adzuna"]
    assert (adzuna["status"], adzuna["error_code"], adzuna["attempts"]) == (
        status,
        error_code,
        attempts,
    )
    assert route.call_count == http_calls  # a refused token means no request was sent
    assert all(key == "adzuna" for key, _ in harness.limiter.calls)
    assert all(
        0 <= wait <= SETTINGS.source_rate_limit_max_wait_seconds
        for _, wait in harness.limiter.calls
    )


# --- filters the source can't apply ----------------------------------------------------------


async def test_unsupported_filters_are_reported_not_ignored(harness: Harness) -> None:
    await harness.enable("adzuna")
    search_id = await create_search(
        harness.client, location=GB, work_formats=["remote"], experience_levels=["senior"]
    )

    with respx.mock() as mock:
        mock.get(ADZUNA_PAGE_1).respond(200, json=adzuna_payload())
        job = await harness.run_and_wait(search_id)

    count = job["result_count"]
    assert count == 4
    adzuna = by_key(job)["adzuna"]
    for name in ("work_formats", "experience_levels"):
        assert adzuna["filters"][name] == {
            "mode": "unsupported",
            "excluded": 0,
            "unverified": count,
        }
    assert adzuna["filters"]["country"]["mode"] == "source"
    warnings = {w["filter"]: w for w in job["filter_warnings"]}
    assert warnings["work_formats"] == {
        "filter": "work_formats",
        "unsupported_by": ["adzuna"],
        "unverified_results": count,
    }
    for item in await harness.results(job["id"]):
        assert {"work_formats", "experience_levels"} <= set(item["unverified_filters"])


async def test_local_filters_exclude_clear_mismatches(harness: Harness) -> None:
    await harness.enable("mock")

    job = await harness.run_and_wait(
        await create_search(harness.client, roles=MOCK_ROLES, work_formats=["remote"])
    )

    report = by_key(job)["mock"]["filters"]["work_formats"]
    assert report == {"mode": "local", "excluded": 2, "unverified": 0}  # hybrid + onsite
    assert job["result_count"] == 3
    assert job["filter_warnings"] == []


async def test_salary_filter_flags_what_it_cannot_compare(harness: Harness) -> None:
    await harness.enable("mock")

    job = await harness.run_and_wait(
        await create_search(
            harness.client, roles=MOCK_ROLES, salary={"min": 4000, "currency": "EUR"}
        )
    )

    # Included: 3000-4500 EUR/month and 50-70 EUR/hour. Unverified: USD (no FX yet) and two
    # postings without a salary. Nothing is guessed, nothing silently passes as a match.
    assert by_key(job)["mock"]["filters"]["salary"] == {
        "mode": "local",
        "excluded": 0,
        "unverified": 3,
    }
    flagged = [
        i
        for i in await harness.results(job["id"], include_inactive="true")
        if i["unverified_filters"]
    ]
    assert len(flagged) == 3 and all(i["unverified_filters"] == ["salary"] for i in flagged)


# --- concurrency ----------------------------------------------------------------------------


async def test_concurrent_run_requests_create_one_run(harness: Harness) -> None:
    await harness.enable("mock")
    recording = harness.use_recording_dispatcher()
    search_id = await create_search(harness.client)

    responses = await asyncio.gather(*(run(harness.client, search_id) for _ in range(10)))

    assert sorted(r.status_code for r in responses) == [200] * 9 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(recording.dispatched) == 1


async def test_concurrent_runs_respect_the_active_limit(harness: Harness) -> None:
    await harness.enable("mock")
    harness.use_recording_dispatcher()
    searches = [await create_search(harness.client) for _ in range(6)]

    responses = await asyncio.gather(*(run(harness.client, s) for s in searches))

    codes = sorted(r.status_code for r in responses)
    assert codes == [202, 202, 409, 409, 409, 409]  # limit: 2 active runs per user
    async with harness.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SearchJob)) == 2


async def test_concurrent_workers_run_a_source_once(harness: Harness) -> None:
    await harness.enable("adzuna")
    harness.use_recording_dispatcher()
    job_id = uuid.UUID(
        (await run(harness.client, await create_search(harness.client, location=GB))).json()["id"]
    )

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=adzuna_payload())

    with respx.mock() as mock:
        route = mock.get(ADZUNA_PAGE_1).mock(side_effect=slow)
        await harness.runner.start(job_id)
        row = await source_row(harness, str(job_id), "adzuna")
        await asyncio.gather(*(harness.runner.run_source(job_id, row.source_id) for _ in range(3)))

    assert route.call_count == 1
    assert (await source_row(harness, str(job_id), "adzuna")).attempts == 1


async def test_abandoned_claim_is_taken_over_and_the_old_worker_is_fenced(
    harness: Harness,
) -> None:
    await harness.enable("mock")
    harness.use_recording_dispatcher()
    job_id = uuid.UUID(
        (await run(harness.client, await create_search(harness.client, roles=MOCK_ROLES))).json()[
            "id"
        ]
    )
    await harness.runner.start(job_id)
    row = await source_row(harness, str(job_id), "mock")
    abandoned = datetime.now(UTC) - timedelta(seconds=SETTINGS.source_run_lease_seconds * 2)
    async with harness.sessions() as db:
        await db.execute(
            update(SearchJobSource)
            .where(SearchJobSource.search_job_id == job_id)
            .values(status="running", attempts=1, started_at=abandoned)
        )
        await db.commit()

    await harness.runner.run_source(job_id, row.source_id)

    taken_over = await source_row(harness, str(job_id), "mock")
    assert (taken_over.status, taken_over.attempts) == ("succeeded", 2)
    async with harness.sessions() as db:
        stale_write = await SearchJobRepository(db).finish_source(
            job_id, row.source_id, attempt=1, values={"fetched_count": 999}
        )
    assert stale_write is False


async def test_stale_worker_cannot_overwrite_a_newer_running_claim(harness: Harness) -> None:
    await harness.enable("mock")
    harness.use_recording_dispatcher()
    job_id = uuid.UUID(
        (await run(harness.client, await create_search(harness.client))).json()["id"]
    )
    await harness.runner.start(job_id)
    row = await source_row(harness, str(job_id), "mock")
    now = datetime.now(UTC)
    lease = timedelta(seconds=SETTINGS.source_run_lease_seconds)
    async with harness.sessions() as db:
        await db.execute(
            update(SearchJobSource)
            .where(SearchJobSource.search_job_id == job_id)
            .values(status="running", attempts=1, started_at=now - 2 * lease)
        )
        repo = SearchJobRepository(db)
        newer = await repo.claim_source(
            job_id,
            row.source_id,
            now=now,
            max_attempts=SETTINGS.source_run_max_attempts,
            retryable_codes=[],
            stale_before=now - lease,
        )
        # The first worker (attempt 1) reports late, while attempt 2 is still running.
        stale = await repo.finish_source(
            job_id, row.source_id, attempt=1, values={"status": "succeeded"}
        )
        current = await repo.finish_source(
            job_id, row.source_id, attempt=2, values={"status": "succeeded"}
        )
        await db.commit()

    assert (newer, stale, current) == (2, False, True)


# --- cancellation, deadline, disabled sources ----------------------------------------------


async def test_cancelled_run_discards_late_results(harness: Harness) -> None:
    await harness.enable("adzuna")
    called, released = asyncio.Event(), asyncio.Event()

    async def held(_request: httpx.Request) -> httpx.Response:
        called.set()
        await released.wait()
        return httpx.Response(200, json=adzuna_payload())

    with respx.mock() as mock:
        mock.get(ADZUNA_PAGE_1).mock(side_effect=held)
        job_id = (
            await run(harness.client, await create_search(harness.client, location=GB))
        ).json()["id"]
        await asyncio.wait_for(called.wait(), timeout=5)
        cancelled = await harness.client.post(
            f"/api/v1/search-jobs/{job_id}/cancel", headers=csrf(harness.client)
        )
        released.set()
        await harness.dispatcher.drain()

    assert cancelled.status_code == 200
    job = await harness.job(job_id)
    assert (job["status"], job["result_count"]) == ("cancelled", 0)
    assert (by_key(job)["adzuna"]["status"], by_key(job)["adzuna"]["error_code"]) == (
        "skipped",
        "SOURCE_RUN_CANCELLED",
    )
    assert await harness.results(job_id) == []
    assert (await harness.client.get("/api/v1/vacancies")).json()["items"] == []
    async with harness.sessions() as db:  # imported as shared data, just not linked
        assert (await db.scalar(select(func.count()).select_from(Vacancy))) == 4


async def test_run_past_its_deadline_calls_no_source(harness: Harness) -> None:
    await harness.enable("adzuna")
    harness.use_recording_dispatcher()
    job_id = (await run(harness.client, await create_search(harness.client, location=GB))).json()[
        "id"
    ]
    async with harness.sessions() as db:
        await db.execute(
            update(SearchJob)
            .where(SearchJob.id == uuid.UUID(job_id))
            .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(ADZUNA_PAGE_1)
        await harness.runner.run(uuid.UUID(job_id))

    job = await harness.job(job_id)
    assert not route.called
    assert job["status"] == "failed"
    assert by_key(job)["adzuna"]["error_code"] == "SOURCE_DEADLINE_EXCEEDED"


async def test_source_disabled_after_creation_is_skipped(harness: Harness) -> None:
    await harness.enable("mock", "adzuna")
    harness.use_recording_dispatcher()
    job_id = (
        await run(
            harness.client, await create_search(harness.client, roles=MOCK_ROLES, location=GB)
        )
    ).json()["id"]
    await harness.disable("adzuna")

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(ADZUNA_PAGE_1)
        await harness.runner.run(uuid.UUID(job_id))

    job = await harness.job(job_id)
    assert not route.called
    assert (by_key(job)["adzuna"]["status"], by_key(job)["adzuna"]["error_code"]) == (
        "skipped",
        "SOURCE_DISABLED",
    )
    assert job["status"] == "completed"  # skipped sources don't count (AMB-23)


async def test_all_sources_disabled_after_creation_fails_the_run(harness: Harness) -> None:
    await harness.enable("mock")
    harness.use_recording_dispatcher()
    job_id = (await run(harness.client, await create_search(harness.client))).json()["id"]
    await harness.disable("mock")

    await harness.runner.run(uuid.UUID(job_id))

    job = await harness.job(job_id)
    assert (job["status"], job["error_code"]) == ("failed", "NO_SOURCES_RAN")
