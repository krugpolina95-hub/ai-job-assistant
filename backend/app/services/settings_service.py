"""The signed-in user's settings and consents (M4.3, PROFILE-003/004, AMB-09).

Two stores, one API: the locale and time zone live on `users` (registration sets them, and
`/auth/me` returns them), the consent flags on the user's `user_profiles` row. Changing a
consent creates that row if needed; it never touches profile data, and deleting the profile
never touches settings.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import PersonalizationData, PersonalizationPurpose, SupportedLocale
from app.core.errors import UnauthenticatedError
from app.models import User
from app.models.profile import DEFAULT_AI_USE_HISTORY, DEFAULT_AI_USE_PROFILE
from app.repositories.profile import UserProfileRepository, UserSkillRepository
from app.repositories.search import SearchRepository
from app.repositories.user import UserRepository
from app.schemas.profile import ProfileOut
from app.schemas.settings import (
    PersonalizationItemOut,
    PersonalizationOut,
    SettingsOut,
    SettingsUpdate,
)
from app.services.profile_service import MATCHING_DATA, ProfileService

logger = structlog.stdlib.get_logger(__name__)

_CONSENTS = ("ai_use_profile", "ai_use_history")


class SettingsService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._users = UserRepository(db)
        self._profiles = UserProfileRepository(db)

    async def get(self, user_id: uuid.UUID) -> SettingsOut:
        user = await self._user(user_id)
        profile = await self._profiles.get_by_user(user_id)
        return SettingsOut(
            preferred_locale=SupportedLocale(user.preferred_locale),
            timezone=user.timezone,
            ai_use_profile=profile.ai_use_profile if profile else DEFAULT_AI_USE_PROFILE,
            ai_use_history=profile.ai_use_history if profile else DEFAULT_AI_USE_HISTORY,
        )

    async def update(self, user_id: uuid.UUID, data: SettingsUpdate) -> SettingsOut:
        changed = data.model_fields_set
        user = await self._user(user_id)
        if data.preferred_locale is not None:
            user.preferred_locale = data.preferred_locale.value
        if data.timezone is not None:
            user.timezone = data.timezone
        if changed & set(_CONSENTS):
            profile = await self._profiles.get_or_create_for_update(user_id)
            if data.ai_use_profile is not None:
                profile.ai_use_profile = data.ai_use_profile
            if data.ai_use_history is not None:
                profile.ai_use_history = data.ai_use_history
        await self._db.commit()
        # Consent changes are worth an audit trail; values are booleans, never personal data.
        logger.info(
            "settings_updated",
            fields=sorted(changed),
            **{name: getattr(data, name) for name in _CONSENTS if name in changed},
        )
        return await self.get(user_id)

    async def personalization(self, user_id: uuid.UUID) -> PersonalizationOut:
        """PROFILE-004: which data the app holds and where it is used under the current
        settings. Matching always uses the profile (no AI; clearing the profile stops it);
        the AI assistant uses it only with `ai_use_profile`, past searches only with
        `ai_use_history`."""
        settings = await self.get(user_id)
        profile = await ProfileService(self._db).get(user_id)
        has_skills = bool(await UserSkillRepository(self._db).list_for_user(user_id))
        has_searches = await SearchRepository(self._db).exists_for_user(user_id)

        profile_purposes = [PersonalizationPurpose.MATCHING]
        if settings.ai_use_profile:
            profile_purposes.append(PersonalizationPurpose.AI_ASSISTANT)
        items = [
            PersonalizationItemOut(
                data=data,
                has_value=_has_value(data, profile, has_skills),
                used_for=list(profile_purposes) if data in MATCHING_DATA else [],
            )
            for data in PersonalizationData
            if data is not PersonalizationData.SEARCH_HISTORY
        ]
        items.append(
            PersonalizationItemOut(
                data=PersonalizationData.SEARCH_HISTORY,
                has_value=has_searches,
                used_for=[PersonalizationPurpose.AI_ASSISTANT] if settings.ai_use_history else [],
            )
        )
        return PersonalizationOut(
            ai_use_profile=settings.ai_use_profile,
            ai_use_history=settings.ai_use_history,
            items=items,
        )

    async def _user(self, user_id: uuid.UUID) -> User:
        user = await self._users.get(user_id)
        if user is None:  # deleted right after its session was checked
            raise UnauthenticatedError()
        return user


def _has_value(data: PersonalizationData, profile: ProfileOut, has_skills: bool) -> bool:
    if data is PersonalizationData.SKILLS:
        return has_skills
    value = getattr(profile, data.value)
    return value not in (None, [], False)
