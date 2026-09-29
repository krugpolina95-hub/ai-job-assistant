"""Settings and consents over HTTP (M4.3, PROFILE-003/004, AMB-09)."""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import PersonalizationData, SearchJobStatus
from app.models import SearchJob, UserProfile
from app.services.profile_service import MATCHING_DATA
from app.services.skill_dictionary_service import SkillDictionaryService
from app.services.source_admin_service import SourceAdminService
from tests.integration.db.search_helpers import RecordingDispatcher, create_search, csrf, run
from tests.integration.db.test_auth_api import client_for, register

pytestmark = pytest.mark.asyncio(loop_scope="session")

DEFAULTS = {
    "preferred_locale": "en",
    "timezone": "UTC",
    "ai_use_profile": True,
    "ai_use_history": False,
}
CHANGED = {
    "preferred_locale": "ru",
    "timezone": "Europe/Vilnius",
    "ai_use_profile": False,
    "ai_use_history": True,
}


async def sign_up(client: AsyncClient, **overrides: Any) -> uuid.UUID:
    response = await register(client, **overrides)
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def settings_of(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/v1/settings")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def patch_settings(client: AsyncClient, body: dict[str, Any]) -> Response:
    return await client.patch("/api/v1/settings", json=body, headers=csrf(client))


async def profile_row(db: AsyncSession, user_id: uuid.UUID) -> UserProfile | None:
    row = await db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
    if row is not None:
        await db.refresh(row)
    return row


# --- read and update ----------------------------------------------------------------------


async def test_new_user_gets_the_registration_values_and_default_consents(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_up(api_client, preferred_locale="ru", timezone="Europe/Vilnius")

    assert await settings_of(api_client) == DEFAULTS | {
        "preferred_locale": "ru",
        "timezone": "Europe/Vilnius",
    }
    assert await profile_row(db_session, user_id) is None  # reading creates nothing


async def test_settings_are_updated_and_shared_with_the_account(api_client: AsyncClient) -> None:
    await sign_up(api_client)

    response = await patch_settings(api_client, CHANGED)

    assert response.status_code == 200, response.text
    assert response.json() == CHANGED == await settings_of(api_client)
    me = (await api_client.get("/api/v1/auth/me")).json()
    assert (me["preferred_locale"], me["timezone"]) == ("ru", "Europe/Vilnius")


@pytest.mark.parametrize(
    "body",
    [
        {"ai_use_history": True},
        {"preferred_locale": "ru"},
        {"timezone": "America/New_York"},
        {"ai_use_profile": False, "timezone": "Asia/Tokyo"},
        {},
    ],
)
async def test_partial_update_changes_only_the_fields_sent(
    api_client: AsyncClient, body: dict[str, Any]
) -> None:
    await sign_up(api_client)

    response = await patch_settings(api_client, body)

    assert response.status_code == 200, response.text
    assert response.json() == DEFAULTS | body


@pytest.mark.parametrize(
    "body",
    [
        {"preferred_locale": "de"},
        {"timezone": "Mars/Olympus_Mons"},
        {"ai_use_profile": "maybe"},
        {"ai_use_profile": None},
        {"timezone": None},
        {"user_id": "00000000-0000-0000-0000-000000000000"},
        {"email": "someone@example.com"},
        {"is_admin": True},
        {"desired_roles": ["Developer"]},
        {"ai_use_history": True, "unknown": 1},
    ],
)
async def test_invalid_or_unknown_fields_change_nothing(
    api_client: AsyncClient, body: dict[str, Any]
) -> None:
    await sign_up(api_client)

    response = await patch_settings(api_client, body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await settings_of(api_client) == DEFAULTS


async def test_settings_are_private_to_their_owner(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession
) -> None:
    owner = await sign_up(api_client)
    await patch_settings(api_client, CHANGED)

    async with client_for(api_app) as other:
        other_id = await sign_up(other)
        assert await settings_of(other) == DEFAULTS
        await patch_settings(other, {"ai_use_profile": True, "preferred_locale": "en"})
        # There is no way to name another user: a user_id in the body is refused.
        refused = await other.patch(
            "/api/v1/settings",
            json={"user_id": str(owner), "ai_use_profile": True},
            headers=csrf(other),
        )
        assert refused.status_code == 422

    assert await settings_of(api_client) == CHANGED
    owner_row = await profile_row(db_session, owner)
    assert owner_row is not None and owner_row.ai_use_profile is False
    other_row = await profile_row(db_session, other_id)
    assert other_row is not None and other_row.ai_use_profile is True


# --- separation from the profile ----------------------------------------------------------


@pytest.mark.parametrize("field", list(CHANGED))
async def test_profile_patch_cannot_change_settings(api_client: AsyncClient, field: str) -> None:
    await sign_up(api_client)

    response = await api_client.patch(
        "/api/v1/profile", json={field: CHANGED[field]}, headers=csrf(api_client)
    )

    assert response.status_code == 422
    assert await settings_of(api_client) == DEFAULTS


async def test_consents_create_no_visible_profile(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_up(api_client)

    await patch_settings(api_client, {"ai_use_profile": False})

    assert await profile_row(db_session, user_id) is not None
    profile = (await api_client.get("/api/v1/profile")).json()
    assert profile["updated_at"] is None
    assert profile["desired_roles"] == [] and profile["location"] is None


async def test_profile_changes_keep_settings(api_client: AsyncClient) -> None:
    await sign_up(api_client)
    await patch_settings(api_client, CHANGED)

    await api_client.patch(
        "/api/v1/profile", json={"desired_roles": ["Developer"]}, headers=csrf(api_client)
    )

    assert await settings_of(api_client) == CHANGED


async def test_deleting_the_profile_keeps_settings(api_client: AsyncClient) -> None:
    await sign_up(api_client)
    await api_client.patch(
        "/api/v1/profile",
        json={"desired_roles": ["Developer"], "summary": "Hello"},
        headers=csrf(api_client),
    )
    await patch_settings(api_client, CHANGED)

    response = await api_client.delete("/api/v1/profile", headers=csrf(api_client))

    assert response.status_code == 204
    assert await settings_of(api_client) == CHANGED
    assert (await api_client.get("/api/v1/profile")).json()["updated_at"] is None


# --- personalization (PROFILE-004) --------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def dictionary(db_session: AsyncSession) -> None:
    await SkillDictionaryService(db_session).sync()


async def personalization(client: AsyncClient) -> dict[str, dict[str, Any]]:
    response = await client.get("/api/v1/settings/personalization")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["ai_use_profile"], body["ai_use_history"]) == (
        (await settings_of(client))["ai_use_profile"],
        (await settings_of(client))["ai_use_history"],
    )
    items: dict[str, dict[str, Any]] = {item["data"]: item for item in body["items"]}
    assert len(items) == len(body["items"])
    return items


async def test_personalization_lists_every_kind_of_data_once(api_client: AsyncClient) -> None:
    await sign_up(api_client)

    items = await personalization(api_client)

    assert set(items) == {data.value for data in PersonalizationData}
    assert not any(item["has_value"] for item in items.values())
    for data in MATCHING_DATA:
        assert items[data.value]["used_for"] == ["matching", "ai_assistant"]
    assert items["display_name"]["used_for"] == items["summary"]["used_for"] == []
    assert items["search_history"]["used_for"] == []


async def test_personalization_follows_settings_and_data(
    api_client: AsyncClient, dictionary: None
) -> None:
    await sign_up(api_client)
    await api_client.patch(
        "/api/v1/profile",
        json={"desired_roles": ["Developer"], "display_name": "Ada", "relocation_ready": True},
        headers=csrf(api_client),
    )
    await api_client.put(
        "/api/v1/profile/skills", json={"skills": [{"name": "Python"}]}, headers=csrf(api_client)
    )
    await create_search(api_client)
    await patch_settings(api_client, {"ai_use_profile": False, "ai_use_history": True})

    items = await personalization(api_client)

    has_value = {name for name, item in items.items() if item["has_value"]}
    assert has_value == {
        "desired_roles",
        "display_name",
        "relocation_ready",
        "skills",
        "search_history",
    }
    assert items["skills"]["used_for"] == ["matching"]
    assert items["search_history"]["used_for"] == ["ai_assistant"]


async def test_personalization_is_per_user(api_client: AsyncClient, api_app: FastAPI) -> None:
    await sign_up(api_client)
    await api_client.patch(
        "/api/v1/profile", json={"desired_roles": ["Developer"]}, headers=csrf(api_client)
    )
    await patch_settings(api_client, {"ai_use_history": True})

    async with client_for(api_app) as other:
        await sign_up(other)
        items = await personalization(other)

    assert items["desired_roles"]["has_value"] is False
    assert items["search_history"]["used_for"] == []


# --- search snapshot ----------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def runnable(api_app: FastAPI, db_session: AsyncSession) -> None:
    api_app.state.dispatcher = RecordingDispatcher()
    admin = SourceAdminService(db_session)
    await admin.sync_definitions()
    await admin.enable("mock", compliance_review_confirmed=True)


async def snapshot_of_new_run(
    client: AsyncClient, db: AsyncSession, search_id: str
) -> dict[str, Any] | None:
    job_id = uuid.UUID((await run(client, search_id)).json()["id"])
    job = await db.get(SearchJob, job_id)
    assert job is not None
    snapshot: dict[str, Any] | None = job.context["profile"]
    await db.execute(
        update(SearchJob).where(SearchJob.id == job_id).values(status=SearchJobStatus.COMPLETED)
    )
    return snapshot


async def test_search_snapshot_carries_the_consent_at_start(
    api_client: AsyncClient, db_session: AsyncSession, runnable: None
) -> None:
    await sign_up(api_client)
    await api_client.patch(
        "/api/v1/profile", json={"desired_roles": ["Developer"]}, headers=csrf(api_client)
    )
    search_id = await create_search(api_client)

    before = await snapshot_of_new_run(api_client, db_session, search_id)
    await patch_settings(api_client, {"ai_use_profile": False})
    after = await snapshot_of_new_run(api_client, db_session, search_id)

    assert before is not None and after is not None
    assert (before["ai_use_profile"], after["ai_use_profile"]) == (True, False)
    assert set(before) == {data.value for data in MATCHING_DATA} | {"ai_use_profile"}


async def test_consents_alone_give_no_profile_snapshot(
    api_client: AsyncClient, db_session: AsyncSession, runnable: None
) -> None:
    await sign_up(api_client)
    await patch_settings(api_client, {"ai_use_profile": False, "ai_use_history": True})

    snapshot = await snapshot_of_new_run(api_client, db_session, await create_search(api_client))

    assert snapshot is None
