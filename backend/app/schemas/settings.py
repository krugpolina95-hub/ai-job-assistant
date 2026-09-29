"""User settings and consents (PROFILE-003/004, AMB-09, GAP-02).

Settings are separate from the profile: the profile describes the person, settings decide how
the app treats them. The locale and time zone are the same values registration sets and
`GET /auth/me` returns, under the same names.
"""

from pydantic import Field, field_validator

from app.core.enums import PersonalizationData, PersonalizationPurpose, SupportedLocale
from app.schemas.auth import TimeZoneName
from app.schemas.base import InputSchema, OutputSchema


class SettingsOut(OutputSchema):
    preferred_locale: SupportedLocale
    timezone: str
    ai_use_profile: bool = Field(description="The AI assistant may use the profile (AMB-09).")
    ai_use_history: bool = Field(description="The AI assistant may use past searches.")


class SettingsUpdate(InputSchema):
    """Partial update: fields left out stay as they are. None of them is nullable."""

    preferred_locale: SupportedLocale | None = None
    timezone: TimeZoneName | None = None
    ai_use_profile: bool | None = None
    ai_use_history: bool | None = None

    @field_validator("preferred_locale", "timezone", "ai_use_profile", "ai_use_history")
    @classmethod
    def not_null(cls, value: object) -> object:
        # Runs only for values that were sent: `None` here is an explicit null.
        if value is None:
            raise ValueError("Must not be null")
        return value


class PersonalizationItemOut(OutputSchema):
    data: PersonalizationData
    has_value: bool = Field(description="Whether the app currently holds this data.")
    used_for: list[PersonalizationPurpose] = Field(
        description="Where it is used under the current settings; empty means stored only."
    )


class PersonalizationOut(OutputSchema):
    ai_use_profile: bool
    ai_use_history: bool
    items: list[PersonalizationItemOut]
