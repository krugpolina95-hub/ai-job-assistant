"""Authentication API against a real database (AUTH-001..004 acceptance criteria)."""

from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.ids import uuid7
from app.core.security import hash_token
from app.main import create_app
from app.models import User, UserSession
from tests.conftest import TEST_LOG_HASH_SECRET
from tests.fakes import install_test_doubles

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "correct horse battery"


def new_email() -> str:
    return f"user-{uuid7().hex}@example.com"


async def register(client: AsyncClient, **overrides: Any) -> Response:
    body = {"email": new_email(), "password": PASSWORD, "accept_terms": True} | overrides
    return await client.post("/api/v1/auth/register", json=body)


async def login(
    client: AsyncClient, email: str, password: str = PASSWORD, **extra: Any
) -> Response:
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password} | extra
    )


def set_cookies(response: Response) -> dict[str, SimpleCookie]:
    """Parsed Set-Cookie headers by cookie name, including attributes."""
    parsed: dict[str, SimpleCookie] = {}
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        parsed.update({name: cookie for name in cookie})
    return parsed


def csrf_header(client: AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


async def sessions_of(db: AsyncSession, user_id: object) -> list[UserSession]:
    result = await db.scalars(select(UserSession).where(UserSession.user_id == user_id))
    return list(result)


# --- registration -------------------------------------------------------------------------


async def test_register_creates_account_and_signs_in(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    email = new_email()

    response = await register(
        api_client, email=email, preferred_locale="ru", timezone="Europe/Vilnius"
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "is_verified", "preferred_locale", "timezone", "created_at"}
    assert (body["email"], body["preferred_locale"], body["timezone"]) == (
        email,
        "ru",
        "Europe/Vilnius",
    )
    assert body["is_verified"] is False

    user = await db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash
    assert (user.terms_version, user.privacy_version) == (
        settings.legal_terms_version,
        settings.legal_privacy_version,
    )
    assert user.terms_accepted_at is not None

    [session] = await sessions_of(db_session, user.id)
    token = api_client.cookies["sid"]
    assert session.token_hash == hash_token(token)
    assert session.token_hash != token


async def test_session_cookies_have_secure_attributes(api_client: AsyncClient) -> None:
    cookies = set_cookies(await register(api_client))

    sid, csrf = cookies["sid"]["sid"], cookies["csrf"]["csrf"]
    assert sid["httponly"] is True
    assert sid["secure"] is True
    assert sid["samesite"].lower() == "lax"
    assert sid["path"] == "/"
    assert sid["max-age"] == ""  # browser-session cookie by default (TD-03)
    assert csrf["httponly"] == ""  # readable by the frontend for double-submit
    assert csrf["secure"] is True


async def test_register_rejects_duplicate_email_case_insensitively(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = new_email()
    await register(api_client, email=email)

    response = await register(
        api_client, email=email.upper().replace("@EXAMPLE.COM", "@example.com")
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert "sid" not in set_cookies(response)
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == email)
    )
    assert count == 1


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"email": "not-an-email"}, "email"),
        ({"password": "too short"}, "password"),
        ({"password": "x" * 129}, "password"),
        ({"accept_terms": False}, "accept_terms"),
        ({"timezone": "Nowhere/Land"}, "timezone"),
        ({"preferred_locale": "xx"}, "preferred_locale"),
        ({"is_admin": True}, "is_admin"),
    ],
)
async def test_register_validates_input_without_echoing_it(
    api_client: AsyncClient, override: dict[str, Any], field: str
) -> None:
    response = await register(api_client, **override)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert ["body", field] in [item["loc"] for item in error["details"]]
    assert "x" * 129 not in response.text
    assert "sid" not in set_cookies(response)


async def test_register_rejects_password_equal_to_email(api_client: AsyncClient) -> None:
    email = new_email()

    response = await register(api_client, email=email, password=email.upper())

    assert response.status_code == 422
    assert email.upper() not in response.text


# --- login --------------------------------------------------------------------------------


async def test_login_succeeds_and_rotates_the_session(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = new_email()
    user_id = (await register(api_client, email=email)).json()["id"]
    registration_token = api_client.cookies["sid"]

    response = await login(api_client, email.upper().replace("@EXAMPLE.COM", "@example.com"))

    assert response.status_code == 200
    assert response.json()["email"] == email
    assert api_client.cookies["sid"] != registration_token
    [session] = await sessions_of(db_session, user_id)  # the replaced session is gone
    assert session.token_hash == hash_token(api_client.cookies["sid"])


async def test_login_with_remember_me_sets_a_persistent_cookie(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    email = new_email()
    await register(api_client, email=email)

    response = await login(api_client, email, remember_me=True)

    max_age = int(set_cookies(response)["sid"]["sid"]["max-age"])
    expected = settings.session_absolute_timeout.total_seconds()
    assert expected - 60 <= max_age <= expected
    session = await db_session.scalar(
        select(UserSession).where(UserSession.token_hash == hash_token(api_client.cookies["sid"]))
    )
    assert session is not None
    assert session.expires_at > datetime.now(UTC) + timedelta(days=29)


async def test_invalid_credentials_are_indistinguishable(api_client: AsyncClient) -> None:
    email = new_email()
    await register(api_client, email=email)
    api_client.cookies.clear()

    wrong_password = await login(api_client, email, "wrong password!")
    unknown_email = await login(api_client, new_email())

    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        assert "sid" not in set_cookies(response)
    assert wrong_password.json()["error"] | {"request_id": None} == unknown_email.json()[
        "error"
    ] | {"request_id": None}
    assert wrong_password.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert wrong_password.json()["error"]["message"] == "Invalid email or password"


async def test_login_upgrades_outdated_password_hash(
    api_client: AsyncClient,
    db_connection: AsyncConnection,
    db_session: AsyncSession,
    settings: Settings,
) -> None:
    email = new_email()
    await register(api_client, email=email)
    stronger = settings.model_copy(update={"password_hash_memory_kib": 2048})
    app = api_app_with(stronger, db_connection)

    async with client_for(app) as client:
        assert (await login(client, email)).status_code == 200

    user = await db_session.scalar(select(User).where(User.email == email))
    assert user is not None
    await db_session.refresh(user)
    assert "m=2048" in user.password_hash


async def test_cross_site_form_post_cannot_log_in(api_client: AsyncClient) -> None:
    email = new_email()
    await register(api_client, email=email)
    api_client.cookies.clear()

    response = await api_client.post(
        "/api/v1/auth/login", data={"email": email, "password": PASSWORD}
    )

    assert response.status_code == 422
    assert "sid" not in set_cookies(response)


# --- authenticated requests ---------------------------------------------------------------


async def test_me_returns_the_current_user(api_client: AsyncClient) -> None:
    registered = (await register(api_client)).json()

    response = await api_client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == registered


async def test_me_without_session_is_unauthenticated(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_me_with_unknown_token_is_unauthenticated(api_client: AsyncClient) -> None:
    api_client.cookies.set("sid", "forged-token-value", domain="testserver")

    response = await api_client.get("/api/v1/auth/me")

    assert response.status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"last_seen_at": datetime.now(UTC) - timedelta(days=8)},
    ],
    ids=["absolute-expiry", "idle-expiry"],
)
async def test_expired_session_is_rejected_and_removed(
    api_client: AsyncClient, db_session: AsyncSession, change: dict[str, datetime]
) -> None:
    user_id = (await register(api_client)).json()["id"]
    await db_session.execute(
        update(UserSession).where(UserSession.user_id == user_id).values(**change)
    )

    response = await api_client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert await sessions_of(db_session, user_id) == []


async def test_activity_slides_the_idle_expiry(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = (await register(api_client)).json()["id"]
    stale = datetime.now(UTC) - timedelta(minutes=10)
    await db_session.execute(
        update(UserSession).where(UserSession.user_id == user_id).values(last_seen_at=stale)
    )

    assert (await api_client.get("/api/v1/auth/me")).status_code == 200

    [session] = await sessions_of(db_session, user_id)
    await db_session.refresh(session)
    assert session.last_seen_at > stale + timedelta(minutes=9)


async def test_me_reissues_a_missing_csrf_cookie(api_client: AsyncClient) -> None:
    await register(api_client)
    api_client.cookies.delete("csrf")

    response = await api_client.get("/api/v1/auth/me")

    assert "csrf" in set_cookies(response)


# --- logout and CSRF ----------------------------------------------------------------------


async def test_logout_requires_the_csrf_header(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = (await register(api_client)).json()["id"]

    missing = await api_client.post("/api/v1/auth/logout")
    mismatched = await api_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "guess"})

    for response in (missing, mismatched):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"
    assert len(await sessions_of(db_session, user_id)) == 1


async def test_logout_ends_the_session(api_client: AsyncClient, db_session: AsyncSession) -> None:
    user_id = (await register(api_client)).json()["id"]

    response = await api_client.post("/api/v1/auth/logout", headers=csrf_header(api_client))

    assert response.status_code == 204
    cleared = set_cookies(response)
    assert cleared["sid"]["sid"]["max-age"] == "0"
    assert cleared["csrf"]["csrf"]["max-age"] == "0"
    assert await sessions_of(db_session, user_id) == []
    assert (await api_client.get("/api/v1/auth/me")).status_code == 401


async def test_logging_out_one_device_keeps_the_others(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession
) -> None:
    email = new_email()
    user_id = (await register(api_client, email=email)).json()["id"]
    async with client_for(api_app) as other_device:
        await login(other_device, email)

        await api_client.post("/api/v1/auth/logout", headers=csrf_header(api_client))

        assert (await other_device.get("/api/v1/auth/me")).status_code == 200
    assert len(await sessions_of(db_session, user_id)) == 1


# --- logging ------------------------------------------------------------------------------


async def test_auth_logs_contain_no_credentials_or_raw_ids(api_client: AsyncClient) -> None:
    email = new_email()
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        user_id = (await register(api_client, email=email)).json()["id"]
        await login(api_client, email, "wrong password!")
        await login(api_client, email)
        await api_client.get("/api/v1/auth/me")

    events = {entry["event"] for entry in logs}
    assert {"user_registered", "login_failed", "login_succeeded"} <= events
    dumped = str(logs)
    assert email not in dumped
    assert PASSWORD not in dumped
    assert "wrong password!" not in dumped
    assert user_id not in dumped
    assert TEST_LOG_HASH_SECRET not in dumped
    assert all(len(entry["user"]) == 16 for entry in logs if "user" in entry)


# --- helpers for tests needing a second app or client ---------------------------------------


def api_app_with(settings: Settings, connection: AsyncConnection) -> FastAPI:
    app = create_app(settings)
    app.state.db_session_factory = async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    install_test_doubles(app)
    return app


def client_for(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")
