"""Search-run creation and reads over HTTP (TD-12, TD-25). Runs are recorded, not executed."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import SearchJobStatus, SearchStatus
from app.models import Search, SearchJob, Source
from app.services.source_admin_service import SourceAdminService
from tests.conftest import build_test_settings
from tests.integration.db.search_helpers import (
    RecordingDispatcher,
    create_search,
    csrf,
    run,
    sign_in,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

DEADLINE_SECONDS = 77


@pytest.fixture
def settings() -> Settings:
    return build_test_settings(
        search_job_deadline_seconds=DEADLINE_SECONDS,
        search_max_active_jobs_per_user=2,
        search_max_runs_per_hour=3,
    )


@pytest.fixture
def dispatcher(api_app: FastAPI) -> RecordingDispatcher:
    recording = RecordingDispatcher()
    api_app.state.dispatcher = recording
    return recording


async def enable(db: AsyncSession, *keys: str) -> None:
    admin = SourceAdminService(db)
    await admin.sync_definitions()
    for key in keys:
        await admin.enable(key, compliance_review_confirmed=True)


async def finish(db: AsyncSession, job_id: str, status: SearchJobStatus) -> None:
    await db.execute(update(SearchJob).where(SearchJob.id == job_id).values(status=status))


# --- searches -----------------------------------------------------------------------------


async def test_create_and_read_a_search(api_client: AsyncClient) -> None:
    await sign_in(api_client)

    search_id = await create_search(
        api_client, salary={"min": 3000, "currency": "eur"}, location={"country": "lt"}
    )
    body = (await api_client.get(f"/api/v1/searches/{search_id}")).json()

    assert body["status"] == "confirmed"
    assert body["params"]["salary"]["currency"] == "EUR"
    assert body["params"]["location"]["country"] == "LT"
    assert body["params_schema_version"] == 1


async def test_invalid_search_is_rejected(api_client: AsyncClient) -> None:
    await sign_in(api_client)

    for params in (
        {"roles": []},
        {"roles": ["x"], "salary": {"min": 5, "max": 1, "currency": "EUR"}},
    ):
        response = await api_client.post(
            "/api/v1/searches", json={"params": params}, headers=csrf(api_client)
        )
        assert response.status_code == 422


# --- creating runs --------------------------------------------------------------------------


async def test_run_creates_a_pending_job_with_a_configurable_deadline(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    search_id = await create_search(api_client)

    response = await run(api_client, search_id, key="click-1")

    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "pending"
    assert [s["key"] for s in job["sources"]] == ["mock"]  # Adzuna is registered but disabled
    assert job["progress"] == {"sources_total": 1, "sources_done": 0, "results_so_far": 0}
    created = datetime.fromisoformat(job["created_at"])
    deadline = datetime.fromisoformat(job["deadline_at"])
    assert abs((deadline - created).total_seconds() - DEADLINE_SECONDS) < 2
    assert [str(j) for j in dispatcher.dispatched] == [job["id"]]
    adzuna = await db_session.scalar(select(Source).where(Source.adapter_key == "adzuna"))
    assert adzuna is not None and adzuna.is_enabled is False


async def test_idempotency_key_replay_returns_the_same_run(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    search_id = await create_search(api_client)

    first = await run(api_client, search_id, key="click-1")
    replay = await run(api_client, search_id, key="click-1")

    assert (first.status_code, replay.status_code) == (202, 200)
    assert replay.json()["id"] == first.json()["id"]
    assert len(dispatcher.dispatched) == 1


async def test_idempotency_key_cannot_be_reused_for_another_search(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    first, second = await create_search(api_client), await create_search(api_client)
    await run(api_client, first, key="click-1")

    response = await run(api_client, second, key="click-1")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_a_search_has_one_active_run(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    search_id = await create_search(api_client)

    first = await run(api_client, search_id)
    again = await run(api_client, search_id, key="another-click")

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    await finish(db_session, first.json()["id"], SearchJobStatus.COMPLETED)
    rerun = await run(api_client, search_id)
    assert rerun.status_code == 202 and rerun.json()["id"] != first.json()["id"]


async def test_active_runs_per_user_are_limited(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    searches = [await create_search(api_client) for _ in range(3)]

    statuses = [(await run(api_client, s)).status_code for s in searches[:2]]
    third = await run(api_client, searches[2])

    assert statuses == [202, 202]
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "JOB_LIMIT_REACHED"


async def test_runs_per_hour_are_limited(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    search_id = await create_search(api_client)
    for _ in range(3):
        job = (await run(api_client, search_id)).json()
        await finish(db_session, job["id"], SearchJobStatus.COMPLETED)

    response = await run(api_client, search_id)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"


async def test_limits_are_per_user(
    api_client: AsyncClient,
    api_app: FastAPI,
    db_session: AsyncSession,
    dispatcher: RecordingDispatcher,
) -> None:
    from httpx import ASGITransport

    await sign_in(api_client)
    await enable(db_session, "mock")
    for _ in range(2):
        await run(api_client, await create_search(api_client))

    async with AsyncClient(
        transport=ASGITransport(app=api_app), base_url="https://testserver"
    ) as other:
        await sign_in(other)
        assert (await run(other, await create_search(other))).status_code == 202


async def test_no_enabled_source_means_no_run(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session)  # registered, none enabled
    search_id = await create_search(api_client)

    response = await run(api_client, search_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_SOURCES_ENABLED"
    assert dispatcher.dispatched == []


async def test_unconfirmed_search_cannot_run(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    search_id = await create_search(api_client)
    await db_session.execute(
        update(Search).where(Search.id == search_id).values(status=SearchStatus.DRAFT)
    )

    response = await run(api_client, search_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SEARCH_NOT_CONFIRMED"


# --- reading, cancelling, deadline --------------------------------------------------------


async def test_etag_returns_304_while_unchanged(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    job_id = (await run(api_client, await create_search(api_client))).json()["id"]

    first = await api_client.get(f"/api/v1/search-jobs/{job_id}")
    unchanged = await api_client.get(
        f"/api/v1/search-jobs/{job_id}", headers={"If-None-Match": first.headers["ETag"]}
    )
    await finish(db_session, job_id, SearchJobStatus.RUNNING)
    changed = await api_client.get(
        f"/api/v1/search-jobs/{job_id}", headers={"If-None-Match": first.headers["ETag"]}
    )

    assert unchanged.status_code == 304 and unchanged.content == b""
    assert changed.status_code == 200 and changed.json()["status"] == "running"


async def test_cancel_is_repeatable_and_final(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    job_id = (await run(api_client, await create_search(api_client))).json()["id"]
    url = f"/api/v1/search-jobs/{job_id}/cancel"

    first = await api_client.post(url, headers=csrf(api_client))
    again = await api_client.post(url, headers=csrf(api_client))

    assert first.status_code == again.status_code == 200
    body = again.json()
    assert body["status"] == "cancelled" and body["finished_at"]
    assert body["sources"][0]["status"] == "skipped"
    assert body["sources"][0]["error_code"] == "SOURCE_RUN_CANCELLED"


async def test_finished_run_cannot_be_cancelled(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    job_id = (await run(api_client, await create_search(api_client))).json()["id"]
    await finish(db_session, job_id, SearchJobStatus.COMPLETED)

    response = await api_client.post(
        f"/api/v1/search-jobs/{job_id}/cancel", headers=csrf(api_client)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JOB_ALREADY_FINISHED"


async def test_overdue_run_is_finalized_when_read(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    job_id = (await run(api_client, await create_search(api_client))).json()["id"]
    await db_session.execute(
        update(SearchJob)
        .where(SearchJob.id == job_id)
        .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    body = (await api_client.get(f"/api/v1/search-jobs/{job_id}")).json()

    assert (body["status"], body["error_code"]) == ("failed", "ALL_SOURCES_FAILED")
    assert body["sources"][0]["status"] == "timed_out"
    assert body["sources"][0]["error_code"] == "SOURCE_DEADLINE_EXCEEDED"
    assert body["progress"]["sources_done"] == 1


async def test_runs_of_a_search_are_listed_newest_first(
    api_client: AsyncClient, db_session: AsyncSession, dispatcher: RecordingDispatcher
) -> None:
    await sign_in(api_client)
    await enable(db_session, "mock")
    search_id = await create_search(api_client)
    ids = []
    for _ in range(3):
        ids.append((await run(api_client, search_id)).json()["id"])
        await finish(db_session, ids[-1], SearchJobStatus.COMPLETED)

    first = (await api_client.get(f"/api/v1/searches/{search_id}/jobs", params={"limit": 2})).json()
    rest = (
        await api_client.get(
            f"/api/v1/searches/{search_id}/jobs", params={"cursor": first["next_cursor"]}
        )
    ).json()

    assert [j["id"] for j in first["items"] + rest["items"]] == list(reversed(ids))
    assert rest["next_cursor"] is None


# --- isolation between users ----------------------------------------------------------------


async def test_other_users_cannot_see_or_touch_a_run(
    api_client: AsyncClient,
    api_app: FastAPI,
    db_session: AsyncSession,
    dispatcher: RecordingDispatcher,
) -> None:
    from httpx import ASGITransport

    await sign_in(api_client)
    await enable(db_session, "mock")
    search_id = await create_search(api_client)
    job_id = (await run(api_client, search_id)).json()["id"]

    async with AsyncClient(
        transport=ASGITransport(app=api_app), base_url="https://testserver"
    ) as other:
        await sign_in(other)
        responses = [
            await other.get(f"/api/v1/searches/{search_id}"),
            await other.get(f"/api/v1/searches/{search_id}/jobs"),
            await run(other, search_id),
            await other.get(f"/api/v1/search-jobs/{job_id}"),
            await other.get(f"/api/v1/search-jobs/{job_id}/results"),
            await other.post(f"/api/v1/search-jobs/{job_id}/cancel", headers=csrf(other)),
        ]

    assert [r.status_code for r in responses] == [404] * 6
    assert (await api_client.get(f"/api/v1/search-jobs/{job_id}")).json()["status"] == "pending"
