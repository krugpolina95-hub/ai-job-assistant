"""Skill dictionary sync and profile row creation against the database."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Skill, SkillAlias, UserProfile
from app.schemas.profile import ProfileUpdate
from app.services.profile_service import ProfileService
from app.services.skill_dictionary_service import SkillDictionaryService, load_dictionary
from tests.integration.db.factories import make_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def test_dictionary_sync_is_idempotent(db_session: AsyncSession) -> None:
    service = SkillDictionaryService(db_session)
    entries = load_dictionary()

    first = await service.sync()
    second = await service.sync()

    assert first.skills_created == len(entries)
    assert second.skills_created == 0
    assert await count(db_session, Skill) == len(entries)
    assert await count(db_session, SkillAlias) == sum(len(e.aliases) for e in entries)


async def test_dictionary_sync_applies_edits(db_session: AsyncSession, tmp_path: Path) -> None:
    path = tmp_path / "skills.json"
    path.write_text('[{"name": "Golang", "aliases": {"en": ["go lang", "gol"]}}]', encoding="utf-8")
    service = SkillDictionaryService(db_session)
    await service.sync(path)
    path.write_text(
        '[{"name": "Golang", "aliases": {"en": ["go lang"], "ru": ["голанг"]}}, {"name": "Rust"}]',
        encoding="utf-8",
    )

    result = await service.sync(path)

    assert result.skills_created == 1
    aliases = dict(
        (await db_session.execute(select(SkillAlias.alias_normalized, SkillAlias.locale))).all()
    )
    assert aliases == {"go lang": "en", "голанг": "ru"}


async def test_concurrent_first_saves_create_one_profile(
    committed_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with committed_sessions() as db:
        user = await make_user(db)
        await db.commit()

    async def save(roles: list[str]) -> None:
        async with committed_sessions() as db:
            await ProfileService(db).update(user.id, ProfileUpdate(desired_roles=roles))

    await asyncio.gather(*(save([f"role {n}"]) for n in range(5)))

    async with committed_sessions() as db:
        rows = list(await db.scalars(select(UserProfile).where(UserProfile.user_id == user.id)))
    assert len(rows) == 1
