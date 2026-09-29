"""Authorization matrix, part 1 (M3.9): which routes need a session, and CSRF coverage.

Every `/api/v1` route is protected unless listed in PUBLIC_ROUTES. New routes are picked up
automatically, so forgetting the auth dependency fails this test. (Part 2, cross-user 404s,
is added with the first user-owned resource endpoints.)
"""

import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_auth, get_current_auth_with_csrf
from app.api.internal.auth import verify_internal_request
from app.core.ids import uuid7
from app.db.session import create_db_engine, create_session_factory
from app.main import create_app
from tests.conftest import build_test_settings
from tests.fakes import install_test_doubles

PUBLIC_ROUTES = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/ready"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"),
}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

SETTINGS = build_test_settings()
APP = create_app(SETTINGS)


def api_routes(app: FastAPI) -> list[tuple[str, str, APIRoute]]:
    # FastAPI keeps included routers nested; iter_route_contexts yields the effective routes.
    routes = [
        (method, context.path, context.route)
        for context in iter_route_contexts(app.routes)
        if isinstance(context.route, APIRoute) and context.path.startswith("/api/v1")
        for method in context.methods or ()
    ]
    return sorted(routes, key=lambda item: (item[1], item[0]))


def dependency_calls(dependant: Dependant) -> set[Callable[..., Any]]:
    calls: set[Callable[..., Any]] = set()
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            calls.add(dependency.call)
        calls |= dependency_calls(dependency)
    return calls


PROTECTED = [(m, p) for m, p, _ in api_routes(APP) if (m, p) not in PUBLIC_ROUTES]


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(SETTINGS)
    # No request below reaches the database: authentication fails on the missing cookie first.
    app.state.db_session_factory = create_session_factory(create_db_engine(SETTINGS))
    install_test_doubles(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as c:
        yield c


def test_public_route_list_is_not_stale() -> None:
    existing = {(method, path) for method, path, _ in api_routes(APP)}

    assert existing >= PUBLIC_ROUTES
    assert PROTECTED, "expected at least one protected route"


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
async def test_protected_route_rejects_requests_without_a_session(
    client: AsyncClient, method: str, path: str
) -> None:
    url = re.sub(r"\{[^}]+\}", str(uuid7()), path)

    response = await client.request(method, url, json={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=[f"{m} {p}" for m, p in PROTECTED])
def test_every_protected_route_depends_on_authentication(method: str, path: str) -> None:
    route = next(r for m, p, r in api_routes(APP) if (m, p) == (method, path))

    assert get_current_auth in dependency_calls(route.dependant)


def test_every_unsafe_protected_route_checks_csrf() -> None:
    unsafe_protected = [
        (method, path, route)
        for method, path, route in api_routes(APP)
        if method in UNSAFE_METHODS and get_current_auth in dependency_calls(route.dependant)
    ]

    assert unsafe_protected, "expected at least one unsafe protected route (logout)"
    for method, path, route in unsafe_protected:
        assert get_current_auth_with_csrf in dependency_calls(route.dependant), f"{method} {path}"


def test_every_internal_route_verifies_the_caller() -> None:
    """n8n's door (/internal/v1): each route needs the service token + HMAC, never a session."""
    internal = [
        (method, context.path, context.route)
        for context in iter_route_contexts(APP.routes)
        if isinstance(context.route, APIRoute) and context.path.startswith("/internal/")
        for method in context.methods or ()
    ]

    assert len(internal) >= 5
    for method, path, route in internal:
        assert verify_internal_request in dependency_calls(route.dependant), f"{method} {path}"
        assert not route.include_in_schema, f"{method} {path} is in the public API document"
