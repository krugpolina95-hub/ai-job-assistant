"""The user's professional profile and skills (PROFILE-001..002, GAP-14).

Each user has at most one profile row, created on the first save of the profile or of a
consent setting. A profile without data reads as empty (`updated_at: null`), so clients need
no special case. Consent flags (`ai_use_profile`, `ai_use_history`) share the row but belong
to the settings (`SettingsService`): profile updates and deletion never touch them.
"""

import uuid
from typing import Any

import structlog
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import EmploymentType, LocationSource, PersonalizationData, WorkFormat
from app.core.errors import ValidationFailedError
from app.models import UserProfile, UserSkill
from app.models.profile import DEFAULT_AI_USE_PROFILE
from app.repositories.profile import SkillRepository, UserProfileRepository, UserSkillRepository
from app.schemas.profile import (
    LanguageOut,
    ProfileLocationOut,
    ProfileOut,
    ProfileSalaryOut,
    ProfileUpdate,
    SkillListOut,
    SkillOut,
    SkillSearchParams,
    UserSkillOut,
    UserSkillsIn,
    UserSkillsOut,
)
from app.services.normalization import normalize_skill

logger = structlog.stdlib.get_logger(__name__)

# Profile fields a user controls, with their empty values (what DELETE /profile restores).
_CLEARED: dict[str, Any] = {
    "display_name": None,
    "desired_roles": [],
    "country": None,
    "region": None,
    "city": None,
    "location_source": None,
    "salary_min": None,
    "salary_max": None,
    "salary_currency": None,
    "salary_period": None,
    "work_formats": [],
    "employment_types": [],
    "experience_level": None,
    "languages": [],
    "relocation_ready": False,
    "summary": None,
}

# The profile data a search run snapshots for matching (architecture §6.3). Name and summary
# are left out: matching doesn't need them. GET /settings/personalization reports this list.
MATCHING_DATA = (
    PersonalizationData.DESIRED_ROLES,
    PersonalizationData.SKILLS,
    PersonalizationData.EXPERIENCE_LEVEL,
    PersonalizationData.LANGUAGES,
    PersonalizationData.SALARY,
    PersonalizationData.WORK_FORMATS,
    PersonalizationData.EMPLOYMENT_TYPES,
    PersonalizationData.RELOCATION_READY,
    PersonalizationData.LOCATION,
)


def has_profile_data(profile: UserProfile | None) -> bool:
    return profile is not None and any(
        getattr(profile, field) != empty for field, empty in _CLEARED.items()
    )


class ProfileService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._profiles = UserProfileRepository(db)
        self._user_skills = UserSkillRepository(db)
        self._skills = SkillRepository(db)

    async def get(self, user_id: uuid.UUID) -> ProfileOut:
        return _profile_out(await self._profiles.get_by_user(user_id))

    async def update(self, user_id: uuid.UUID, data: ProfileUpdate) -> ProfileOut:
        profile = await self._profiles.get_or_create_for_update(user_id)
        for field in data.model_fields_set:
            match field:
                case "location":
                    location = data.location
                    profile.country = location.country if location else None
                    profile.region = location.region if location else None
                    profile.city = location.city if location else None
                    # A location typed by the user outranks any estimate (CON-10).
                    profile.location_source = LocationSource.MANUAL if location else None
                case "salary":
                    salary = data.salary
                    profile.salary_min = salary.min if salary else None
                    profile.salary_max = salary.max if salary else None
                    profile.salary_currency = salary.currency if salary else None
                    profile.salary_period = salary.period if salary else None
                case "languages":
                    profile.languages = [item.model_dump(mode="json") for item in data.languages]
                case "work_formats" | "employment_types":
                    setattr(profile, field, [value.value for value in getattr(data, field)])
                case _:
                    setattr(profile, field, getattr(data, field))
        await self._db.commit()
        await self._db.refresh(profile)
        logger.info("profile_updated", fields=sorted(data.model_fields_set))
        return _profile_out(profile)

    async def clear(self, user_id: uuid.UUID) -> None:
        """Removes every profile field and skill (the user's right to delete their data).
        Consent settings stay as they are."""
        profile = await self._profiles.get_by_user(user_id)
        if profile is not None:
            for field, value in _CLEARED.items():
                setattr(profile, field, value)
        await self._user_skills.delete_all_for_user(user_id)
        await self._db.commit()
        logger.info("profile_cleared")

    async def get_skills(self, user_id: uuid.UUID) -> UserSkillsOut:
        return _skills_out(await self._user_skills.list_for_user(user_id))

    async def replace_skills(self, user_id: uuid.UUID, data: UserSkillsIn) -> UserSkillsOut:
        """Stores exactly the given skills. Names and aliases are resolved against the skill
        dictionary; unknown or repeated skills reject the whole request."""
        keys = [normalize_skill(item.name) for item in data.skills]
        known = await self._skills.resolve(set(keys))
        problems: list[dict[str, Any]] = []
        seen: set[uuid.UUID] = set()
        for index, key in enumerate(keys):
            skill = known.get(key)
            if skill is None:
                problems.append(_skill_problem(index, "unknown_skill", "Not a known skill"))
            elif skill.id in seen:
                problems.append(_skill_problem(index, "duplicate_skill", "Skill listed twice"))
            else:
                seen.add(skill.id)
        if problems:
            raise ValidationFailedError("Invalid skills", details=problems)

        await self._user_skills.delete_all_for_user(user_id)
        for item, key in zip(data.skills, keys, strict=True):
            self._user_skills.add(
                UserSkill(
                    user_id=user_id,
                    skill_id=known[key].id,
                    skill_level=item.level,
                    years_experience=item.years_experience,
                )
            )
        await self._db.commit()
        logger.info("profile_skills_replaced", count=len(keys))
        return await self.get_skills(user_id)

    async def search_skills(self, params: SkillSearchParams) -> SkillListOut:
        skills = await self._skills.search(normalize_skill(params.q), params.limit)
        return SkillListOut(items=[SkillOut(id=s.id, name=s.name) for s in skills])

    async def search_snapshot(self, user_id: uuid.UUID) -> dict[str, Any] | None:
        """The profile subset a search run keeps in its context (architecture §6.3), so
        profile edits apply to the next search and never to one that is running. It holds
        exactly `MATCHING_DATA`, plus `ai_use_profile`, so the AI context builder (M7) can
        honour the consent."""
        profile = await self._profiles.get_by_user(user_id)
        skills = await self._user_skills.list_for_user(user_id)
        if not has_profile_data(profile) and not skills:
            return None
        out = _profile_out(profile)
        data: dict[PersonalizationData, Any] = {
            PersonalizationData.DESIRED_ROLES: out.desired_roles,
            PersonalizationData.SKILLS: [
                {
                    "name": item.skill.name,
                    "level": item.skill_level,
                    "years_experience": item.years_experience,
                }
                for item in skills
            ],
            PersonalizationData.EXPERIENCE_LEVEL: out.experience_level,
            PersonalizationData.LANGUAGES: out.languages,
            PersonalizationData.SALARY: out.salary,
            PersonalizationData.WORK_FORMATS: out.work_formats,
            PersonalizationData.EMPLOYMENT_TYPES: out.employment_types,
            PersonalizationData.RELOCATION_READY: out.relocation_ready,
            PersonalizationData.LOCATION: out.location,
        }
        snapshot: dict[str, Any] = to_jsonable_python(
            {key.value: data[key] for key in MATCHING_DATA}
            | {"ai_use_profile": profile.ai_use_profile if profile else DEFAULT_AI_USE_PROFILE}
        )
        return snapshot


def _profile_out(profile: UserProfile | None) -> ProfileOut:
    if profile is None or not has_profile_data(profile):
        return ProfileOut(
            display_name=None,
            desired_roles=[],
            location=None,
            salary=None,
            work_formats=[],
            employment_types=[],
            experience_level=None,
            languages=[],
            relocation_ready=False,
            summary=None,
            updated_at=None,
        )
    has_location = any((profile.country, profile.region, profile.city))
    return ProfileOut(
        display_name=profile.display_name,
        desired_roles=profile.desired_roles,
        location=ProfileLocationOut(
            country=profile.country,
            region=profile.region,
            city=profile.city,
            source=profile.location_source,
        )
        if has_location
        else None,
        salary=ProfileSalaryOut(
            min=profile.salary_min,
            max=profile.salary_max,
            currency=profile.salary_currency,
            period=profile.salary_period,
        )
        if profile.salary_currency and profile.salary_period
        else None,
        work_formats=[WorkFormat(value) for value in profile.work_formats],
        employment_types=[EmploymentType(value) for value in profile.employment_types],
        experience_level=profile.experience_level,
        languages=[LanguageOut.model_validate(item) for item in profile.languages],
        relocation_ready=profile.relocation_ready,
        summary=profile.summary,
        updated_at=profile.updated_at,
    )


def _skills_out(items: list[UserSkill]) -> UserSkillsOut:
    return UserSkillsOut(
        items=[
            UserSkillOut(
                skill=SkillOut(id=item.skill.id, name=item.skill.name),
                level=item.skill_level,
                years_experience=item.years_experience,
            )
            for item in items
        ]
    )


def _skill_problem(index: int, error_type: str, message: str) -> dict[str, Any]:
    """Same shape as request validation errors, so clients can mark the row."""
    return {"loc": ["body", "skills", index, "name"], "type": error_type, "message": message}
