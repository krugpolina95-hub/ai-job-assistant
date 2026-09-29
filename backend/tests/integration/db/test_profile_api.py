"""Profile and profile skills over HTTP (PROFILE-001..002, GAP-14, architecture §6.3)."""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SearchJob, Skill, UserProfile, UserSkill
from app.services.skill_dictionary_service import SkillDictionaryService
from app.services.source_admin_service import SourceAdminService
from tests.integration.db.search_helpers import (
    RecordingDispatcher,
    create_search,
    csrf,
    run,
    sign_in,
)
from tests.integration.db.test_auth_api import client_for

pytestmark = pytest.mark.asyncio(loop_scope="session")

FULL_PROFILE: dict[str, Any] = {
    "display_name": "Ada",
    "desired_roles": ["Backend Developer", "Data Engineer"],
    "location": {"country": "lt", "city": "Vilnius"},
    "salary": {"min": "3000.50", "max": 4500, "currency": "eur"},
    "work_formats": ["remote", "hybrid"],
    "employment_types": ["full_time"],
    "experience_level": "middle",
    "languages": [{"code": "en", "level": "C1"}, {"code": "lt", "level": "native"}],
    "relocation_ready": True,
    "summary": "Backend developer with five years of Python.",
}


@pytest_asyncio.fixture(loop_scope="session")
async def dictionary(db_session: AsyncSession) -> None:
    await SkillDictionaryService(db_session).sync()


async def patch(client: AsyncClient, body: dict[str, Any]) -> Response:
    return await client.patch("/api/v1/profile", json=body, headers=csrf(client))


async def put_skills(client: AsyncClient, *skills: dict[str, Any]) -> Response:
    return await client.put(
        "/api/v1/profile/skills", json={"skills": list(skills)}, headers=csrf(client)
    )


async def profile(client: AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/v1/profile")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- profile ------------------------------------------------------------------------------


async def test_new_user_has_an_empty_profile(api_client: AsyncClient) -> None:
    await sign_in(api_client)

    assert await profile(api_client) == {
        "display_name": None,
        "desired_roles": [],
        "location": None,
        "salary": None,
        "work_formats": [],
        "employment_types": [],
        "experience_level": None,
        "languages": [],
        "relocation_ready": False,
        "summary": None,
        "updated_at": None,
    }


async def test_profile_is_saved_and_read_back(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)

    response = await patch(api_client, FULL_PROFILE)

    assert response.status_code == 200, response.text
    body = await profile(api_client)
    assert body == response.json()
    assert body["location"] == {
        "country": "LT",
        "region": None,
        "city": "Vilnius",
        "source": "manual",
    }
    assert body["salary"] == {
        "min": "3000.50",
        "max": "4500.00",
        "currency": "EUR",
        "period": "month",
    }
    assert body["languages"] == FULL_PROFILE["languages"]
    assert body["updated_at"] is not None
    stored = await db_session.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
    assert stored is not None
    assert stored.work_formats == ["remote", "hybrid"]


async def test_partial_update_changes_only_the_fields_sent(api_client: AsyncClient) -> None:
    await sign_in(api_client)
    await patch(api_client, FULL_PROFILE)

    response = await patch(api_client, {"experience_level": "senior", "summary": None})

    body = response.json()
    assert (body["experience_level"], body["summary"]) == ("senior", None)
    unchanged = {k: v for k, v in body.items() if k not in {"experience_level", "summary"}}
    before = {k: v for k, v in (await profile(api_client)).items() if k in unchanged}
    assert unchanged == before
    assert body["desired_roles"] == FULL_PROFILE["desired_roles"]
    assert body["location"]["city"] == "Vilnius"


async def test_null_clears_location_and_salary(api_client: AsyncClient) -> None:
    await sign_in(api_client)
    await patch(api_client, FULL_PROFILE)

    body = (await patch(api_client, {"location": None, "salary": None})).json()

    assert (body["location"], body["salary"]) == (None, None)


@pytest.mark.parametrize(
    "body",
    [
        {"salary": {"min": 5000, "max": 3000, "currency": "EUR"}},
        {"location": {"country": "Lithuania"}},
        {"languages": [{"code": "en", "level": "C1"}, {"code": "en", "level": "B1"}]},
        {"work_formats": ["office"]},
        {"age": 35},
        {"photo": "https://example.com/me.jpg"},
        {"ai_use_profile": False},
    ],
)
async def test_invalid_updates_are_rejected_and_nothing_changes(
    api_client: AsyncClient, body: dict[str, Any]
) -> None:
    await sign_in(api_client)
    await patch(api_client, FULL_PROFILE)
    before = await profile(api_client)

    response = await patch(api_client, body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await profile(api_client) == before


async def test_delete_clears_profile_and_skills_but_keeps_consent_and_account(
    api_client: AsyncClient, db_session: AsyncSession, dictionary: None
) -> None:
    user_id = await sign_in(api_client)
    await patch(api_client, FULL_PROFILE)
    await put_skills(api_client, {"name": "Python"})
    await db_session.execute(
        update(UserProfile).where(UserProfile.user_id == user_id).values(ai_use_profile=False)
    )

    response = await api_client.delete("/api/v1/profile", headers=csrf(api_client))

    assert response.status_code == 204
    body = await profile(api_client)
    assert body["desired_roles"] == [] and body["location"] is None and body["summary"] is None
    assert (await api_client.get("/api/v1/profile/skills")).json() == {"items": []}
    stored = await db_session.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.ai_use_profile is False
    assert (await api_client.get("/api/v1/auth/me")).status_code == 200
    # Deleting again is harmless.
    assert (await api_client.delete("/api/v1/profile", headers=csrf(api_client))).status_code == 204


async def test_profiles_are_private_to_their_owner(
    api_client: AsyncClient, api_app: FastAPI, dictionary: None
) -> None:
    await sign_in(api_client)
    await patch(api_client, FULL_PROFILE)
    await put_skills(api_client, {"name": "Python"})

    async with client_for(api_app) as other:
        await sign_in(other)
        assert (await profile(other))["display_name"] is None
        assert (await other.get("/api/v1/profile/skills")).json() == {"items": []}
        await patch(other, {"display_name": "Mallory"})
        await other.delete("/api/v1/profile", headers=csrf(other))

    mine = await profile(api_client)
    assert mine["display_name"] == "Ada"
    assert len((await api_client.get("/api/v1/profile/skills")).json()["items"]) == 1


# --- skills -------------------------------------------------------------------------------


async def test_skills_are_resolved_through_names_and_aliases(
    api_client: AsyncClient, dictionary: None
) -> None:
    await sign_in(api_client)

    response = await put_skills(
        api_client,
        {"name": "питон", "level": "advanced", "years_experience": 5},
        {"name": "k8s"},
        {"name": "управление проектами", "years_experience": "1.5"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == (await api_client.get("/api/v1/profile/skills")).json()
    items = {item["skill"]["name"]: item for item in response.json()["items"]}
    assert list(items) == ["Kubernetes", "Project Management", "Python"]
    assert (items["Python"]["level"], items["Python"]["years_experience"]) == ("advanced", "5.0")
    assert items["Project Management"]["years_experience"] == "1.5"


async def test_put_replaces_the_whole_list(api_client: AsyncClient, dictionary: None) -> None:
    await sign_in(api_client)
    await put_skills(api_client, {"name": "Python"}, {"name": "SQL"})

    await put_skills(api_client, {"name": "Go"})
    assert [
        i["skill"]["name"] for i in (await put_skills(api_client, {"name": "Go"})).json()["items"]
    ] == ["Go"]

    assert (await put_skills(api_client)).json() == {"items": []}


async def test_unknown_or_repeated_skills_reject_the_request(
    api_client: AsyncClient, db_session: AsyncSession, dictionary: None
) -> None:
    user_id = await sign_in(api_client)
    await put_skills(api_client, {"name": "SQL"})

    response = await put_skills(
        api_client,
        {"name": "Python"},
        {"name": "Underwater basket weaving"},
        {"name": "py"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {
            "loc": ["body", "skills", 1, "name"],
            "type": "unknown_skill",
            "message": "Not a known skill",
        },
        {
            "loc": ["body", "skills", 2, "name"],
            "type": "duplicate_skill",
            "message": "Skill listed twice",
        },
    ]
    stored = await db_session.scalars(
        select(Skill.name).join(UserSkill).where(UserSkill.user_id == user_id)
    )
    assert list(stored) == ["SQL"]


async def test_skill_lookup_for_autocomplete(api_client: AsyncClient, dictionary: None) -> None:
    await sign_in(api_client)

    async def lookup(q: str, **params: Any) -> list[str]:
        response = await api_client.get("/api/v1/skills", params={"q": q, **params})
        assert response.status_code == 200, response.text
        return [item["name"] for item in response.json()["items"]]

    assert (await lookup("py"))[0] == "Python"
    assert "PyTorch" in await lookup("py")
    assert await lookup("ПИТОН") == ["Python"]
    assert await lookup("c++") == ["C++"]
    assert await lookup("%") == []
    assert len(await lookup("s", limit=3)) == 3
    assert (await api_client.get("/api/v1/skills", params={"q": ""})).status_code == 422


# --- the profile in search runs -----------------------------------------------------------


async def test_search_runs_snapshot_the_profile_at_start(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession, dictionary: None
) -> None:
    api_app.state.dispatcher = RecordingDispatcher()
    admin = SourceAdminService(db_session)
    await admin.sync_definitions()
    await admin.enable("mock", compliance_review_confirmed=True)
    await sign_in(api_client)
    await patch(api_client, FULL_PROFILE)
    await put_skills(api_client, {"name": "Python", "level": "expert", "years_experience": 5})
    search_id = await create_search(api_client)

    first = (await run(api_client, search_id)).json()["id"]
    await db_session.execute(
        update(SearchJob).where(SearchJob.id == uuid.UUID(first)).values(status="completed")
    )
    await patch(api_client, {"desired_roles": ["Platform Engineer"]})
    second = (await run(api_client, search_id)).json()["id"]

    async def snapshot(job_id: str) -> dict[str, Any]:
        job = await db_session.get(SearchJob, uuid.UUID(job_id))
        assert job is not None
        await db_session.refresh(job)
        value: dict[str, Any] = job.context["profile"]
        return value

    earlier, later = await snapshot(first), await snapshot(second)
    assert earlier["desired_roles"] == ["Backend Developer", "Data Engineer"]
    assert later["desired_roles"] == ["Platform Engineer"]
    assert earlier["skills"] == [{"name": "Python", "level": "expert", "years_experience": "5.0"}]
    assert earlier["salary"]["currency"] == "EUR"
    assert earlier["location"]["country"] == "LT"
    assert earlier["ai_use_profile"] is True
    assert "display_name" not in earlier and "summary" not in earlier


async def test_search_without_a_profile_has_no_profile_context(
    api_client: AsyncClient, api_app: FastAPI, db_session: AsyncSession
) -> None:
    api_app.state.dispatcher = RecordingDispatcher()
    admin = SourceAdminService(db_session)
    await admin.sync_definitions()
    await admin.enable("mock", compliance_review_confirmed=True)
    await sign_in(api_client)

    job_id = (await run(api_client, await create_search(api_client))).json()["id"]

    job = await db_session.get(SearchJob, uuid.UUID(job_id))
    assert job is not None
    assert job.context["profile"] is None
