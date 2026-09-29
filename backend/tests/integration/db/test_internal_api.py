"""The n8n → backend internal API (M6.1, TD-06, architecture §8.3) and the n8n dispatcher
(M6.2, TD-09), against a real database. n8n itself is replaced by signed test requests and a
respx-mocked webhook."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import SearchJobSourceStatus, SearchJobStatus
from app.core.security import request_signature, signature_is_valid
from app.integrations.n8n.webhook import N8nWebhookClient
from app.models import SearchJob, SearchJobSource
from app.services.dispatch import N8nDispatcher
from app.services.job_runner import SearchJobRunner
from app.services.source_admin_service import SourceAdminService
from tests.conftest import build_test_settings
from tests.integration.db.search_helpers import (
    RecordingDispatcher,
    ScriptedLimiter,
    create_search,
    csrf,
    run,
    sign_in,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

TOKEN = "internal-token-" + "t" * 32
SECRET = "internal-hmac-" + "h" * 32
WEBHOOK_URL = "http://n8n.test:5678/webhook/wf-001-search"
WEBHOOK_SECRET = "webhook-secret-" + "w" * 32


@pytest.fixture
def settings() -> Settings:
    return build_test_settings(internal_api_token=TOKEN, internal_hmac_secret=SECRET)


@pytest.fixture
def runnable(api_app: FastAPI, settings: Settings) -> RecordingDispatcher:
    """Runs are created through the public API and recorded, not executed: the tests drive
    them through the internal API the way n8n does."""
    dispatcher = RecordingDispatcher()
    api_app.state.dispatcher = dispatcher
    api_app.state.job_runner = SearchJobRunner(
        api_app.state.db_session_factory, settings, ScriptedLimiter()
    )
    return dispatcher


async def enable_mock(db: AsyncSession) -> None:
    admin = SourceAdminService(db)
    await admin.sync_definitions()
    await admin.enable("mock", compliance_review_confirmed=True)


async def call(
    client: AsyncClient,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str = TOKEN,
    secret: str = SECRET,
    clock_offset: int = 0,
    signed_path: str | None = None,
) -> Response:
    content = json.dumps(body).encode() if body is not None else b""
    stamp = str(int(time.time()) + clock_offset)  # taken at call time, not at collection
    signature = request_signature(
        secret, timestamp=stamp, method=method, path=signed_path or path, body=content
    )
    headers = {"Authorization": f"Bearer {token}", "X-Timestamp": stamp, "X-Signature": signature}
    if body is not None:
        headers["Content-Type"] = "application/json"
    return await client.request(method, path, content=content, headers=headers)


async def new_run(client: AsyncClient, db: AsyncSession) -> str:
    await enable_mock(db)
    await sign_in(client)
    response = await run(client, await create_search(client))
    assert response.status_code == 202, response.text
    return str(response.json()["id"])


# --- authentication -----------------------------------------------------------------------


async def test_a_signed_request_is_accepted(
    api_client: AsyncClient, db_session: AsyncSession, runnable: RecordingDispatcher
) -> None:
    job_id = await new_run(api_client, db_session)

    response = await call(api_client, "GET", f"/internal/v1/jobs/{job_id}/context")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "job_id": job_id,
        "status": "pending",
        "deadline_at": response.json()["deadline_at"],
        "sources": ["mock"],
    }  # no search parameters or profile: n8n needs no user data


@pytest.mark.parametrize(
    "tamper",
    [
        {"token": "wrong-token-" + "x" * 32},
        {"secret": "wrong-secret-" + "x" * 32},
        {"clock_offset": -301},  # too old
        {"clock_offset": 301},  # from the future
        {"signed_path": "/internal/v1/maintenance/sweep-jobs"},  # replayed elsewhere
    ],
)
async def test_bad_credentials_are_refused_before_anything_runs(
    api_client: AsyncClient, runnable: RecordingDispatcher, tamper: dict[str, Any]
) -> None:
    response = await call(
        api_client, "POST", "/internal/v1/jobs/" + str(uuid.uuid4()) + "/finalize", **tamper
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_missing_headers_are_refused(api_client: AsyncClient) -> None:
    path = f"/internal/v1/jobs/{uuid.uuid4()}/context"
    for headers in (
        {},
        {"Authorization": f"Bearer {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}", "X-Timestamp": str(int(time.time()))},
        {"Authorization": TOKEN, "X-Timestamp": "1", "X-Signature": "sha256=0"},
    ):
        assert (await api_client.get(path, headers=headers)).status_code == 401


async def test_a_tampered_body_is_refused(
    api_client: AsyncClient, db_session: AsyncSession, runnable: RecordingDispatcher
) -> None:
    job_id = await new_run(api_client, db_session)
    path = f"/internal/v1/jobs/{job_id}/status"
    stamp = str(int(time.time()))
    signature = request_signature(
        SECRET, timestamp=stamp, method="POST", path=path, body=b'{"status":"running"}'
    )

    response = await api_client.post(
        path,
        content=b'{"status":"running" }',
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Timestamp": stamp,
            "X-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401


async def test_without_configured_secrets_everything_is_refused(
    api_app: FastAPI, runnable: RecordingDispatcher
) -> None:
    api_app.state.settings = build_test_settings()  # no INTERNAL_API_TOKEN/HMAC secret
    async with AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="https://testserver"
    ) as client:
        response = await call(client, "POST", "/internal/v1/maintenance/sweep-jobs")

    assert response.status_code == 401


async def test_internal_routes_are_not_in_the_public_api_document(api_client: AsyncClient) -> None:
    paths = (await api_client.get("/api/v1/openapi.json")).json()["paths"]

    assert not [path for path in paths if path.startswith("/internal")]


# --- the run, step by step, as n8n drives it ----------------------------------------------


async def test_n8n_drives_a_run_to_completion(
    api_client: AsyncClient, db_session: AsyncSession, runnable: RecordingDispatcher
) -> None:
    job_id = await new_run(api_client, db_session)
    base = f"/internal/v1/jobs/{job_id}"

    started = await call(api_client, "POST", f"{base}/status", {"status": "running"})
    again = await call(api_client, "POST", f"{base}/status", {"status": "running"})
    ran = await call(api_client, "POST", f"{base}/sources/mock/run")
    repeated = await call(api_client, "POST", f"{base}/sources/mock/run")
    finalized = await call(api_client, "POST", f"{base}/finalize")

    assert started.json() == again.json() == {"job_id": job_id, "status": "running"}
    assert ran.status_code == 200, ran.text
    outcome = ran.json()
    assert (outcome["source"], outcome["status"], outcome["attempts"]) == ("mock", "succeeded", 1)
    assert outcome["stored_count"] > 0 and outcome["error_code"] is None
    assert repeated.json() == outcome  # idempotent: not fetched again
    assert finalized.json()["status"] == "completed"
    assert finalized.json()["result_count"] == outcome["stored_count"]
    public = (await api_client.get(f"/api/v1/search-jobs/{job_id}")).json()
    assert (public["status"], public["result_count"]) == ("completed", outcome["stored_count"])
    assert (await call(api_client, "POST", f"{base}/finalize")).json() == finalized.json()


async def test_finalize_waits_for_unfinished_sources(
    api_client: AsyncClient, db_session: AsyncSession, runnable: RecordingDispatcher
) -> None:
    job_id = await new_run(api_client, db_session)
    await call(api_client, "POST", f"/internal/v1/jobs/{job_id}/status", {"status": "running"})

    response = await call(api_client, "POST", f"/internal/v1/jobs/{job_id}/finalize")

    assert response.json()["status"] == "running"


async def test_a_cancelled_run_tells_n8n_to_stop(
    api_client: AsyncClient, db_session: AsyncSession, runnable: RecordingDispatcher
) -> None:
    job_id = await new_run(api_client, db_session)
    await api_client.post(f"/api/v1/search-jobs/{job_id}/cancel", headers=csrf(api_client))

    status = await call(
        api_client, "POST", f"/internal/v1/jobs/{job_id}/status", {"status": "running"}
    )
    source = await call(api_client, "POST", f"/internal/v1/jobs/{job_id}/sources/mock/run")

    for response in (status, source):
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "JOB_NOT_RUNNING"
    row = await db_session.scalar(
        select(SearchJobSource).where(SearchJobSource.search_job_id == uuid.UUID(job_id))
    )
    assert row is not None and row.attempts == 0  # the source was never asked


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/internal/v1/jobs/{job}/sources/adzuna/run", 404),  # not part of this run
        ("/internal/v1/jobs/{other}/sources/mock/run", 404),
        ("/internal/v1/jobs/{other}/finalize", 404),
        ("/internal/v1/jobs/{job}/sources/Mock!/run", 422),
        ("/internal/v1/jobs/not-a-uuid/finalize", 422),
    ],
)
async def test_unknown_runs_and_sources(
    api_client: AsyncClient,
    db_session: AsyncSession,
    runnable: RecordingDispatcher,
    path: str,
    status: int,
) -> None:
    job_id = await new_run(api_client, db_session)
    await call(api_client, "POST", f"/internal/v1/jobs/{job_id}/status", {"status": "running"})

    response = await call(api_client, "POST", path.format(job=job_id, other=uuid.uuid4()))

    assert response.status_code == status, response.text


async def test_invalid_status_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, runnable: RecordingDispatcher
) -> None:
    job_id = await new_run(api_client, db_session)

    for body in ({"status": "completed"}, {"status": "running", "extra": 1}):
        response = await call(api_client, "POST", f"/internal/v1/jobs/{job_id}/status", body)
        assert response.status_code == 422


async def test_sweep_finishes_overdue_runs(
    api_client: AsyncClient, db_session: AsyncSession, runnable: RecordingDispatcher
) -> None:
    job_id = await new_run(api_client, db_session)
    await db_session.execute(
        update(SearchJob)
        .where(SearchJob.id == uuid.UUID(job_id))
        .values(deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    first = await call(api_client, "POST", "/internal/v1/maintenance/sweep-jobs")
    second = await call(api_client, "POST", "/internal/v1/maintenance/sweep-jobs")

    assert (first.json(), second.json()) == ({"finalized": 1}, {"finalized": 0})
    job = await db_session.get(SearchJob, uuid.UUID(job_id))
    assert job is not None
    await db_session.refresh(job)
    assert (job.status, job.error_code) == (SearchJobStatus.FAILED, "ALL_SOURCES_FAILED")


# --- the n8n dispatcher (backend → n8n) ---------------------------------------------------


async def no_wait(_seconds: float) -> None:
    return None


def use_n8n(api_app: FastAPI, http: httpx.AsyncClient) -> None:
    webhook = N8nWebhookClient(http, url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
    api_app.state.dispatcher = N8nDispatcher(
        webhook, api_app.state.db_session_factory, sleep=no_wait
    )


async def run_through_n8n(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession, *replies: Any
) -> tuple[dict[str, Any], respx.Route]:
    await enable_mock(db_session)
    await sign_in(api_client)
    search_id = await create_search(api_client)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(WEBHOOK_URL).mock(side_effect=list(replies))
        async with httpx.AsyncClient(follow_redirects=False) as http:
            use_n8n(api_app, http)
            response = await run(api_client, search_id)
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body, route


async def test_dispatch_sends_a_signed_trigger(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession
) -> None:
    job, route = await run_through_n8n(
        api_client, api_app, db_session, httpx.Response(200, json={"ok": True})
    )

    assert job["status"] == "pending"  # n8n owns it from here
    [sent] = route.calls
    request = sent.request
    assert json.loads(request.content)["job_id"] == job["id"]
    assert signature_is_valid(
        WEBHOOK_SECRET,
        request.headers["X-Signature"],
        timestamp=request.headers["X-Timestamp"],
        method="POST",
        path="/webhook/wf-001-search",
        body=request.content,
        now=time.time(),
        max_age_seconds=300,
    )


async def test_a_brief_n8n_hiccup_is_retried(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession
) -> None:
    job, route = await run_through_n8n(
        api_client, api_app, db_session, httpx.Response(502), httpx.Response(200)
    )

    assert job["status"] == "pending"
    assert route.call_count == 2


@pytest.mark.parametrize(
    "replies",
    [
        [httpx.Response(500), httpx.Response(503)],
        [httpx.ConnectError("refused"), httpx.ConnectError("refused")],
        [httpx.Response(302, headers={"Location": "https://evil.test"})] * 2,  # not followed
        [httpx.Response(401), httpx.Response(401)],
    ],
)
async def test_an_undeliverable_run_fails_at_once(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession, replies: list[Any]
) -> None:
    job, route = await run_through_n8n(api_client, api_app, db_session, *replies)

    assert route.call_count == 2
    assert (job["status"], job["error_code"]) == ("failed", "DISPATCH_ERROR")
    assert {s["status"] for s in job["sources"]} == {SearchJobSourceStatus.SKIPPED.value}
    assert {s["error_code"] for s in job["sources"]} == {"SOURCE_NOT_RUN"}
