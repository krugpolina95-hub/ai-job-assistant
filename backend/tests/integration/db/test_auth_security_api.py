"""Brute-force protection, security events, password change and reset over HTTP
(AUTH-004, AUTH-006, TD-03, architecture §4.4)."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import AuthEventType
from app.core.security import hash_token
from app.models import AuthEvent, PasswordResetToken, User, UserSession
from tests.conftest import build_test_settings
from tests.fakes import InMemoryRateLimiter, RecordingEmailSender
from tests.integration.db.test_auth_api import (
    PASSWORD,
    client_for,
    csrf_header,
    login,
    new_email,
    register,
    sessions_of,
    set_cookies,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

NEW_PASSWORD = "a brand new passphrase"
WRONG = "wrong password!"
TOKEN_IN_LINK = re.compile(r"#token=([A-Za-z0-9_-]+)")


@pytest.fixture
def settings() -> Settings:
    # One trusted proxy: tests choose the client address with X-Forwarded-For.
    return build_test_settings(trusted_proxy_count=1)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limiter(api_app: FastAPI, clock: Clock) -> InMemoryRateLimiter:
    """The real rules, in memory. Without this fixture API tests run without request limits."""
    limiter = InMemoryRateLimiter(clock)
    api_app.state.auth_rate_limiter = limiter
    return limiter


@pytest.fixture
def outbox(api_app: FastAPI) -> RecordingEmailSender:
    sender: RecordingEmailSender = api_app.state.email_sender
    return sender


def from_ip(ip: str) -> dict[str, str]:
    return {"X-Forwarded-For": ip}


async def user_by_email(db: AsyncSession, email: str) -> User:
    user = await db.scalar(select(User).where(User.email == email))
    assert user is not None
    await db.refresh(user)
    return user


async def events(db: AsyncSession, *types: AuthEventType) -> list[AuthEvent]:
    statement = select(AuthEvent).order_by(AuthEvent.created_at, AuthEvent.id)
    if types:
        statement = statement.where(AuthEvent.event_type.in_(types))
    return list(await db.scalars(statement))


async def fresh_account(client: AsyncClient, **overrides: Any) -> str:
    email = new_email()
    response = await register(client, email=email, **overrides)
    assert response.status_code == 201, response.text
    client.cookies.clear()
    return email


def assert_rate_limited(response: Response, rule: str) -> None:
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.json()["error"]["details"] == {"limit": rule}
    assert int(response.headers["Retry-After"]) >= 1
    assert "sid" not in set_cookies(response)


def comparable(response: Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error | {"request_id": None}


# --- request limits -----------------------------------------------------------------------


async def test_failed_logins_are_limited_per_ip_and_email(
    api_client: AsyncClient, db_session: AsyncSession, limiter: InMemoryRateLimiter
) -> None:
    email = await fresh_account(api_client)
    for _ in range(5):
        assert (await login(api_client, email, WRONG)).status_code == 401

    # Even the right password waits: the attacker can't tell a hit from a miss.
    assert_rate_limited(await login(api_client, email), "login_per_ip_and_email")

    [event] = await events(db_session, AuthEventType.RATE_LIMITED)
    assert event.detail == "login_per_ip_and_email"
    assert event.email_hash is not None


async def test_login_limit_is_per_address_and_email(
    api_client: AsyncClient, limiter: InMemoryRateLimiter
) -> None:
    email = await fresh_account(api_client)
    other = await fresh_account(api_client)
    for _ in range(5):
        await login(api_client, email, WRONG)

    assert (
        await api_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": PASSWORD},
            headers=from_ip("198.51.100.20"),
        )
    ).status_code == 200
    api_client.cookies.clear()
    assert (await login(api_client, other)).status_code == 200


async def test_successful_logins_do_not_count(
    api_client: AsyncClient, limiter: InMemoryRateLimiter
) -> None:
    email = await fresh_account(api_client)

    for _ in range(8):
        assert (await login(api_client, email)).status_code == 200


async def test_failed_logins_are_limited_per_ip_across_emails(
    api_client: AsyncClient, limiter: InMemoryRateLimiter
) -> None:
    for _ in range(20):
        assert (await login(api_client, new_email(), WRONG)).status_code == 401

    assert_rate_limited(await login(api_client, new_email()), "login_per_ip")


async def test_login_limit_expires(
    api_client: AsyncClient, limiter: InMemoryRateLimiter, clock: Clock
) -> None:
    email = await fresh_account(api_client)
    for _ in range(5):
        await login(api_client, email, WRONG)
    limited = await login(api_client, email)
    assert limited.status_code == 429

    clock.now += int(limited.headers["Retry-After"])

    assert (await login(api_client, email)).status_code == 200


async def test_registration_is_limited_per_ip(
    api_client: AsyncClient, limiter: InMemoryRateLimiter
) -> None:
    for _ in range(5):
        assert (await register(api_client)).status_code == 201

    assert_rate_limited(await register(api_client), "register_per_ip")
    other_ip = await api_client.post(
        "/api/v1/auth/register",
        json={"email": new_email(), "password": PASSWORD, "accept_terms": True},
        headers=from_ip("198.51.100.30"),
    )
    assert other_ip.status_code == 201


# --- account lockout ----------------------------------------------------------------------


async def test_account_locks_after_repeated_failures(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    email = await fresh_account(api_client)
    for _ in range(settings.login_lockout_threshold):
        assert (await login(api_client, email, WRONG)).status_code == 401

    user = await user_by_email(db_session, email)
    assert user.locked_until is not None
    assert user.locked_until > datetime.now(UTC) + timedelta(minutes=14)
    assert len(await events(db_session, AuthEventType.ACCOUNT_LOCKED)) == 1

    right_password = await login(api_client, email)
    wrong_password = await login(api_client, email, WRONG)
    unknown_email = await login(api_client, new_email())
    assert right_password.status_code == 401
    assert "sid" not in set_cookies(right_password)
    assert comparable(right_password) == comparable(wrong_password) == comparable(unknown_email)
    # Attempts during the lock don't extend it.
    assert (await user_by_email(db_session, email)).locked_until == user.locked_until


async def test_login_works_again_after_the_lock_expires(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    email = await fresh_account(api_client)
    for _ in range(settings.login_lockout_threshold):
        await login(api_client, email, WRONG)
    await db_session.execute(
        update(User)
        .where(User.email == email)
        .values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
    )

    assert (await login(api_client, email)).status_code == 200
    user = await user_by_email(db_session, email)
    assert (user.failed_login_count, user.locked_until) == (0, None)


async def test_a_successful_login_resets_the_failure_count(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    email = await fresh_account(api_client)
    almost = settings.login_lockout_threshold - 1
    for _ in range(almost):
        await login(api_client, email, WRONG)
    assert (await login(api_client, email)).status_code == 200
    api_client.cookies.clear()
    for _ in range(almost):
        await login(api_client, email, WRONG)

    user = await user_by_email(db_session, email)
    assert (user.failed_login_count, user.locked_until) == (almost, None)


# --- security events ----------------------------------------------------------------------


async def test_events_keep_no_raw_email_or_address(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = new_email()
    unknown = new_email()
    headers = from_ip("203.0.113.9") | {"User-Agent": "test-agent/1.0"}
    await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "accept_terms": True},
        headers=headers,
    )
    for address, password in ((unknown, PASSWORD), (email, WRONG), (email, PASSWORD)):
        await api_client.post(
            "/api/v1/auth/login", json={"email": address, "password": password}, headers=headers
        )

    stored = await events(db_session)
    user = await user_by_email(db_session, email)
    assert [(e.event_type, e.detail, e.user_id) for e in stored] == [
        (AuthEventType.LOGIN_FAILED, "unknown_account", None),
        (AuthEventType.LOGIN_FAILED, "wrong_password", user.id),
        (AuthEventType.LOGIN_SUCCEEDED, None, user.id),
    ]
    assert {e.ip_prefix for e in stored} == {"203.0.113.0/24"}
    assert {e.user_agent for e in stored} == {"test-agent/1.0"}
    assert stored[1].email_hash == stored[2].email_hash != stored[0].email_hash
    dumped = str([vars(e) for e in stored])
    assert email not in dumped
    assert unknown not in dumped
    assert "203.0.113.9" not in dumped


async def test_forged_forwarded_addresses_are_ignored(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Only the entry the trusted proxy appended (the last one) counts.
    await api_client.post(
        "/api/v1/auth/login",
        json={"email": new_email(), "password": WRONG},
        headers=from_ip("6.6.6.6, 203.0.113.9"),
    )

    [event] = await events(db_session)
    assert event.ip_prefix == "203.0.113.0/24"


# --- password change ----------------------------------------------------------------------


async def change_password(
    client: AsyncClient, current: str = PASSWORD, new: str = NEW_PASSWORD
) -> Response:
    return await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": current, "new_password": new},
        headers=csrf_header(client),
    )


async def test_password_change_signs_out_every_session(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession
) -> None:
    email = new_email()
    registered = await register(api_client, email=email)
    old_token = api_client.cookies["sid"]
    async with client_for(api_app) as other_device:
        assert (await login(other_device, email)).status_code == 200

        response = await change_password(api_client)

        assert response.status_code == 204
        new_token = set_cookies(response)["sid"]["sid"].value
        assert new_token != old_token
        assert (await other_device.get("/api/v1/auth/me")).status_code == 401
    assert (await api_client.get("/api/v1/auth/me")).status_code == 200
    stale = client_for(api_app)
    stale.cookies.set("sid", old_token)
    async with stale:
        assert (await stale.get("/api/v1/auth/me")).status_code == 401

    [session] = await sessions_of(db_session, registered.json()["id"])
    assert session.token_hash == hash_token(new_token)
    api_client.cookies.clear()
    assert (await login(api_client, email)).status_code == 401
    assert (await login(api_client, email, NEW_PASSWORD)).status_code == 200
    [event] = await events(db_session, AuthEventType.PASSWORD_CHANGED)
    assert str(event.user_id) == registered.json()["id"]


async def test_password_change_keeps_a_remembered_session_persistent(
    api_client: AsyncClient,
) -> None:
    email = await fresh_account(api_client)
    await login(api_client, email, remember_me=True)

    response = await change_password(api_client)

    cookie = set_cookies(response)["sid"]["sid"]
    assert int(cookie["max-age"]) > 29 * 24 * 3600


async def test_browser_session_stays_a_browser_session(api_client: AsyncClient) -> None:
    await register(api_client)

    response = await change_password(api_client)

    assert set_cookies(response)["sid"]["sid"]["max-age"] == ""


async def test_wrong_current_password_changes_nothing(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = new_email()
    await register(api_client, email=email)
    before = (await user_by_email(db_session, email)).password_hash

    response = await change_password(api_client, current=WRONG)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURRENT_PASSWORD"
    assert (await api_client.get("/api/v1/auth/me")).status_code == 200
    assert (await user_by_email(db_session, email)).password_hash == before
    [event] = await events(db_session, AuthEventType.LOGIN_FAILED)
    assert event.detail == "password_change"


async def test_new_password_must_not_be_the_email(api_client: AsyncClient) -> None:
    email = new_email()
    await register(api_client, email=email)

    response = await change_password(api_client, new=email.upper())

    assert response.status_code == 422
    [problem] = response.json()["error"]["details"]
    assert problem["loc"] == ["body", "new_password"]


async def test_password_change_is_limited_per_user(
    api_client: AsyncClient, limiter: InMemoryRateLimiter
) -> None:
    await register(api_client)
    for _ in range(5):
        assert (await change_password(api_client, current=WRONG)).status_code == 400

    assert_rate_limited(await change_password(api_client), "password_change_per_user")


# --- password reset -----------------------------------------------------------------------


async def forgot(client: AsyncClient, email: str, ip: str | None = None) -> Response:
    return await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": email},
        headers=from_ip(ip) if ip else None,
    )


async def reset(client: AsyncClient, token: str, password: str = NEW_PASSWORD) -> Response:
    return await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": password}
    )


def token_from(outbox: RecordingEmailSender) -> str:
    match = TOKEN_IN_LINK.search(outbox.sent[-1].body)
    assert match is not None
    return match.group(1)


async def test_reset_request_looks_the_same_for_unknown_emails(
    api_client: AsyncClient, db_session: AsyncSession, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)

    known = await forgot(api_client, email)
    unknown = await forgot(api_client, new_email())

    assert known.status_code == unknown.status_code == 202
    assert known.content == unknown.content == b""
    assert [sent.to for sent in outbox.sent] == [email]
    requested = await events(db_session, AuthEventType.PASSWORD_RESET_REQUESTED)
    assert [event.user_id is None for event in requested] == [False, True]


async def test_reset_email_holds_a_single_use_link(
    api_client: AsyncClient, db_session: AsyncSession, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)

    await forgot(api_client, email)

    [sent] = outbox.sent
    token = token_from(outbox)
    assert f"http://localhost/en/reset-password#token={token}" in sent.body
    assert "30" in sent.body
    [stored] = list(await db_session.scalars(select(PasswordResetToken)))
    assert stored.token_hash == hash_token(token)
    assert token not in str(vars(stored))
    remaining = stored.expires_at - datetime.now(UTC)
    assert timedelta(minutes=29) < remaining <= timedelta(minutes=30)


async def test_reset_email_uses_the_account_language(
    api_client: AsyncClient, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client, preferred_locale="ru")

    await forgot(api_client, email)

    assert "пароль" in outbox.sent[0].body
    assert "/ru/reset-password#token=" in outbox.sent[0].body


async def test_reset_sets_the_password_and_signs_out_everywhere(
    api_client: AsyncClient,
    api_app: FastAPI,
    db_session: AsyncSession,
    outbox: RecordingEmailSender,
    settings: Settings,
) -> None:
    email = new_email()
    registered = await register(api_client, email=email)
    for _ in range(settings.login_lockout_threshold):
        await login(client_for(api_app), email, WRONG)
    assert (await user_by_email(db_session, email)).locked_until is not None
    await forgot(api_client, email)

    response = await reset(api_client, token_from(outbox))

    assert response.status_code == 204
    assert (await api_client.get("/api/v1/auth/me")).status_code == 401
    assert await sessions_of(db_session, registered.json()["id"]) == []
    user = await user_by_email(db_session, email)
    assert (user.failed_login_count, user.locked_until) == (0, None)
    assert (await login(api_client, email)).status_code == 401
    assert (await login(api_client, email, NEW_PASSWORD)).status_code == 200
    assert len(await events(db_session, AuthEventType.PASSWORD_RESET)) == 1


async def test_reset_token_works_once(
    api_client: AsyncClient, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)
    await forgot(api_client, email)
    token = token_from(outbox)

    assert (await reset(api_client, token)).status_code == 204
    again = await reset(api_client, token, "yet another passphrase")

    assert again.status_code == 400
    assert again.json()["error"]["code"] == "RESET_TOKEN_INVALID"


async def test_only_the_newest_reset_link_works(
    api_client: AsyncClient, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)
    await forgot(api_client, email)
    first = token_from(outbox)
    await forgot(api_client, email)
    second = token_from(outbox)

    assert (await reset(api_client, first)).status_code == 400
    assert (await reset(api_client, second)).status_code == 204


async def test_expired_or_unknown_tokens_are_rejected_alike(
    api_client: AsyncClient, db_session: AsyncSession, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)
    await forgot(api_client, email)
    token = token_from(outbox)
    await db_session.execute(
        update(PasswordResetToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    expired = await reset(api_client, token)
    unknown = await reset(api_client, "A" * 43)

    assert expired.status_code == unknown.status_code == 400
    assert comparable(expired) == comparable(unknown)
    assert (await reset(api_client, "not a token!")).status_code == 422


async def test_rejected_new_password_keeps_the_token_usable(
    api_client: AsyncClient, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)
    await forgot(api_client, email)
    token = token_from(outbox)

    same_as_email = await reset(api_client, token, email)
    too_short = await reset(api_client, token, "short")

    assert same_as_email.status_code == too_short.status_code == 422
    assert (await reset(api_client, token)).status_code == 204


async def test_reset_requests_are_limited_per_email(
    api_client: AsyncClient, limiter: InMemoryRateLimiter, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)
    for n in range(3):
        assert (await forgot(api_client, email, ip=f"198.51.100.{n}")).status_code == 202

    assert_rate_limited(
        await forgot(api_client, email, ip="198.51.100.99"), "password_reset_request_per_email"
    )
    assert len(outbox.sent) == 3


async def test_reset_requests_are_limited_per_ip(
    api_client: AsyncClient, limiter: InMemoryRateLimiter
) -> None:
    for _ in range(5):
        assert (await forgot(api_client, new_email())).status_code == 202

    assert_rate_limited(await forgot(api_client, new_email()), "password_reset_request_per_ip")


async def test_reset_attempts_are_limited_per_ip(
    api_client: AsyncClient, limiter: InMemoryRateLimiter
) -> None:
    for n in range(10):
        assert (await reset(api_client, f"{n}" * 43)).status_code == 400

    assert_rate_limited(await reset(api_client, "B" * 43), "password_reset_per_ip")


async def test_reset_flow_logs_no_token_or_email(
    api_client: AsyncClient, outbox: RecordingEmailSender
) -> None:
    email = await fresh_account(api_client)
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        await forgot(api_client, email)
        token = token_from(outbox)
        await reset(api_client, token)
        await forgot(api_client, new_email())

    events_logged = [entry["event"] for entry in logs]
    assert "password_reset_requested" in events_logged
    assert "password_reset" in events_logged
    dumped = str(logs)
    assert token not in dumped
    assert email not in dumped
    assert NEW_PASSWORD not in dumped


async def test_reset_session_rows_are_really_gone(
    api_client: AsyncClient, db_session: AsyncSession, outbox: RecordingEmailSender
) -> None:
    email = new_email()
    await register(api_client, email=email)
    await forgot(api_client, email)
    await reset(api_client, token_from(outbox))

    user = await user_by_email(db_session, email)
    remaining = await db_session.scalars(select(UserSession).where(UserSession.user_id == user.id))
    assert list(remaining) == []
