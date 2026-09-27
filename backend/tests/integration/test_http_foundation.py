"""Error envelope, request IDs, access log, body size limit and security headers.

A test-only router exercises each error path, so no business endpoint is needed.
"""

from collections.abc import AsyncIterator

import pytest
import structlog
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import Field

from app.api.middleware import SECURITY_HEADERS
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.main import create_app
from app.schemas.base import InputSchema
from app.schemas.errors import ErrorOut

MAX_BODY = 1024


class EchoIn(InputSchema):
    name: str = Field(min_length=1, max_length=50)
    password: str


probe_router = APIRouter(prefix="/api/v1/_test")


@probe_router.post("/echo")
async def echo(body: EchoIn) -> dict[str, str]:
    return {"name": body.name}


@probe_router.get("/not-found")
async def not_found() -> None:
    raise NotFoundError()


@probe_router.get("/conflict")
async def conflict() -> None:
    raise ConflictError("Search is already running", details={"job_id": "abc"})


@probe_router.get("/rule")
async def rule() -> None:
    raise ValidationFailedError(details=[{"field": "salary_max", "rule": "gte_salary_min"}])


@probe_router.get("/crash")
async def crash() -> None:
    raise RuntimeError("database password is hunter2")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings.model_copy(update={"max_request_body_bytes": MAX_BODY}))
    app.include_router(probe_router)
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def assert_envelope(response_json: dict[str, object], code: str, request_id: str) -> ErrorOut:
    envelope = ErrorOut.model_validate(response_json)
    assert envelope.error.code == code
    assert envelope.error.request_id == request_id
    return envelope


async def test_every_response_gets_a_request_id(client: AsyncClient) -> None:
    first = await client.get("/api/v1/health")
    second = await client.get("/api/v1/health")

    assert first.headers["X-Request-ID"]
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


async def test_well_formed_incoming_request_id_is_reused(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "frontend-123.a_b"})

    assert response.headers["X-Request-ID"] == "frontend-123.a_b"


async def test_malformed_incoming_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "bad id\r\ninjected"})

    assert response.headers["X-Request-ID"] != "bad id\r\ninjected"
    assert response.headers["X-Request-ID"].isalnum()


async def test_security_headers_are_set(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


async def test_unknown_route_returns_not_found_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert_envelope(response.json(), "NOT_FOUND", response.headers["X-Request-ID"])


async def test_wrong_method_returns_envelope_and_allow_header(client: AsyncClient) -> None:
    response = await client.delete("/api/v1/health")

    assert response.status_code == 405
    assert_envelope(response.json(), "METHOD_NOT_ALLOWED", response.headers["X-Request-ID"])
    assert "GET" in response.headers["Allow"]


async def test_domain_errors_map_to_envelope(client: AsyncClient) -> None:
    not_found = await client.get("/api/v1/_test/not-found")
    conflict = await client.get("/api/v1/_test/conflict")
    rule = await client.get("/api/v1/_test/rule")

    assert not_found.status_code == 404
    assert_envelope(not_found.json(), "NOT_FOUND", not_found.headers["X-Request-ID"])
    assert conflict.status_code == 409
    envelope = assert_envelope(conflict.json(), "CONFLICT", conflict.headers["X-Request-ID"])
    assert envelope.error.message == "Search is already running"
    assert envelope.error.details == {"job_id": "abc"}
    assert rule.status_code == 422
    assert_envelope(rule.json(), "VALIDATION_ERROR", rule.headers["X-Request-ID"])


async def test_request_validation_errors_do_not_echo_input(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/_test/echo", json={"name": "", "password": "s3cret-value", "extra": 1}
    )

    assert response.status_code == 422
    envelope = assert_envelope(
        response.json(), "VALIDATION_ERROR", response.headers["X-Request-ID"]
    )
    assert isinstance(envelope.error.details, list)
    locations = {tuple(item.loc) for item in envelope.error.details}  # type: ignore[union-attr]
    assert locations == {("body", "name"), ("body", "extra")}
    assert "s3cret-value" not in response.text


async def test_malformed_json_is_a_validation_error(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/_test/echo", content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert_envelope(response.json(), "VALIDATION_ERROR", response.headers["X-Request-ID"])


async def test_unhandled_exception_returns_generic_500_and_is_logged(
    client: AsyncClient,
) -> None:
    with structlog.testing.capture_logs() as logs:
        response = await client.get("/api/v1/_test/crash")

    assert response.status_code == 500
    envelope = assert_envelope(response.json(), "INTERNAL_ERROR", response.headers["X-Request-ID"])
    assert envelope.error.message == "Internal server error"
    assert "hunter2" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert any(entry["event"] == "unhandled_exception" for entry in logs)


async def test_declared_body_over_limit_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/_test/echo",
        content=b"x" * (MAX_BODY + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert_envelope(response.json(), "PAYLOAD_TOO_LARGE", response.headers["X-Request-ID"])


async def test_streamed_body_over_limit_is_rejected(client: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * (MAX_BODY // 2)

    response = await client.post(
        "/api/v1/_test/echo", content=chunks(), headers={"Content-Type": "application/json"}
    )

    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413
    assert_envelope(response.json(), "PAYLOAD_TOO_LARGE", response.headers["X-Request-ID"])


async def test_body_within_limit_is_accepted(client: AsyncClient) -> None:
    response = await client.post("/api/v1/_test/echo", json={"name": "Ada", "password": "x"})

    assert response.status_code == 200
    assert response.json() == {"name": "Ada"}


async def test_access_log_has_request_id_status_and_duration(client: AsyncClient) -> None:
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        response = await client.get("/api/v1/health?email=ada@example.com")

    [entry] = [e for e in logs if e["event"] == "http_request"]
    assert entry["request_id"] == response.headers["X-Request-ID"]
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/v1/health"
    assert entry["status"] == 200
    assert entry["duration_ms"] >= 0
    assert "ada@example.com" not in str(entry)


async def test_openapi_documents_the_error_envelope(client: AsyncClient) -> None:
    schema = (await client.get("/api/v1/openapi.json")).json()

    assert "ErrorOut" in schema["components"]["schemas"]
    responses = schema["paths"]["/api/v1/health"]["get"]["responses"]
    assert responses["500"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorOut"
    }
