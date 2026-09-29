import uuid
from collections.abc import Collection

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from app.core.ids import uuid7
from app.models import Skill, SkillAlias, UserProfile, UserSkill
from app.repositories.base import SharedRepository, UserOwnedRepository, escape_like


class UserProfileRepository(UserOwnedRepository[UserProfile]):
    model = UserProfile

    async def get_by_user(self, user_id: uuid.UUID) -> UserProfile | None:
        profile: UserProfile | None = await self.session.scalar(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
        return profile

    async def get_or_create_for_update(self, user_id: uuid.UUID) -> UserProfile:
        """The user's profile row, created on first use and locked for this transaction.
        `ON CONFLICT DO NOTHING` makes concurrent first saves safe (one row per user)."""
        await self.session.execute(
            insert(UserProfile)
            .values(id=uuid7(), user_id=user_id)
            .on_conflict_do_nothing(index_elements=[UserProfile.user_id])
        )
        result = await self.session.execute(
            select(UserProfile)
            .where(UserProfile.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one()


class UserSkillRepository(UserOwnedRepository[UserSkill]):
    model = UserSkill

    async def list_for_user(self, user_id: uuid.UUID) -> list[UserSkill]:
        result = await self.session.scalars(
            select(UserSkill)
            .join(Skill)
            .where(UserSkill.user_id == user_id)
            .options(selectinload(UserSkill.skill))
            .order_by(Skill.name)
        )
        return list(result)

    async def delete_all_for_user(self, user_id: uuid.UUID) -> None:
        await self.session.execute(delete(UserSkill).where(UserSkill.user_id == user_id))


class SkillRepository(SharedRepository[Skill]):
    model = Skill

    async def resolve(self, keys: Collection[str]) -> dict[str, Skill]:
        """Dictionary skills by normalized name or alias. Keys must already be normalized."""
        if not keys:
            return {}
        by_name = await self.session.scalars(select(Skill).where(Skill.normalized_name.in_(keys)))
        found: dict[str, Skill] = {skill.normalized_name: skill for skill in by_name}
        aliased = await self.session.execute(
            select(SkillAlias.alias_normalized, Skill)
            .join(Skill, SkillAlias.skill_id == Skill.id)
            .where(SkillAlias.alias_normalized.in_(keys))
        )
        for alias, skill in aliased.all():
            found.setdefault(alias, skill)
        return found

    async def search(self, prefix: str, limit: int) -> list[Skill]:
        """Skills whose name or an alias starts with `prefix` (normalized); exact matches
        first, then shorter names."""
        pattern = f"{escape_like(prefix)}%"
        alias_match = select(SkillAlias.skill_id).where(
            SkillAlias.alias_normalized.like(pattern, escape="\\")
        )
        exact_alias = select(SkillAlias.skill_id).where(SkillAlias.alias_normalized == prefix)
        result = await self.session.scalars(
            select(Skill)
            .where(or_(Skill.normalized_name.like(pattern, escape="\\"), Skill.id.in_(alias_match)))
            .order_by(
                case(
                    (Skill.normalized_name == prefix, 0),
                    (Skill.id.in_(exact_alias), 0),
                    else_=1,
                ),
                func.char_length(Skill.name),
                Skill.name,
            )
            .limit(limit)
        )
        return list(result)

    async def get_by_normalized_name(self, normalized_name: str) -> Skill | None:
        skill: Skill | None = await self.session.scalar(
            select(Skill).where(Skill.normalized_name == normalized_name)
        )
        return skill

    async def aliases(self) -> dict[str, SkillAlias]:
        result = await self.session.scalars(select(SkillAlias))
        return {alias.alias_normalized: alias for alias in result}
