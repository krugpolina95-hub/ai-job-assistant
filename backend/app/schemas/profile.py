"""Professional profile (PROFILE-001..002) and profile skills (GAP-14).

The profile holds only what matching and search personalization need. Sensitive attributes
(age, gender, nationality, religion, health, marital status, photo) are never accepted: unknown
fields are rejected (CLAUDE.md §7, MATCH-004).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, field_validator, model_validator

from app.core.enums import (
    EmploymentType,
    ExperienceLevel,
    LanguageLevel,
    LocationSource,
    SalaryPeriod,
    SkillLevel,
    WorkFormat,
)
from app.schemas.base import InputSchema, OutputSchema
from app.schemas.search import SalaryParams, Term

MAX_PROFILE_SKILLS = 100


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _lower(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


class LanguageIn(InputSchema):
    code: str = Field(pattern=r"^[a-z]{2}$", description="ISO 639-1 code, e.g. `en`.")
    level: LanguageLevel

    _lower_code = field_validator("code", mode="before")(_lower)


class ProfileLocationIn(InputSchema):
    """Where the user lives or wants to work, at city level. No coordinates (TD-15)."""

    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    region: str | None = Field(default=None, min_length=1, max_length=100)
    city: str | None = Field(default=None, min_length=1, max_length=100)

    _upper_country = field_validator("country", mode="before")(_upper)

    @model_validator(mode="after")
    def not_empty(self) -> Self:
        if self.country is None and self.region is None and self.city is None:
            raise ValueError("Give a country, region or city, or send null to clear")
        return self


class ProfileUpdate(InputSchema):
    """Partial update: fields left out stay as they are; `null` clears a nullable field.
    Lists and the salary are replaced as a whole."""

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    desired_roles: list[Term] = Field(default_factory=list, max_length=10)
    location: ProfileLocationIn | None = None
    salary: SalaryParams | None = Field(
        default=None, description="Salary expectation, as in search parameters."
    )
    work_formats: list[WorkFormat] = Field(default_factory=list, max_length=3)
    employment_types: list[EmploymentType] = Field(default_factory=list, max_length=5)
    experience_level: ExperienceLevel | None = None
    languages: list[LanguageIn] = Field(default_factory=list, max_length=20)
    relocation_ready: bool = False
    summary: str | None = Field(default=None, min_length=1, max_length=5000)

    @field_validator("desired_roles")
    @classmethod
    def unique_roles(cls, roles: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for role in roles:
            seen.setdefault(role.casefold(), role)
        return list(seen.values())

    @field_validator("work_formats", "employment_types")
    @classmethod
    def unique_values(cls, values: list[object]) -> list[object]:
        return list(dict.fromkeys(values))

    @field_validator("languages")
    @classmethod
    def one_level_per_language(cls, languages: list[LanguageIn]) -> list[LanguageIn]:
        codes = [language.code for language in languages]
        if len(codes) != len(set(codes)):
            raise ValueError("Each language may appear only once")
        return languages


class ProfileLocationOut(OutputSchema):
    country: str | None
    region: str | None
    city: str | None
    source: LocationSource | None


class ProfileSalaryOut(OutputSchema):
    min: Decimal | None
    max: Decimal | None
    currency: str
    period: SalaryPeriod


class LanguageOut(OutputSchema):
    code: str
    level: LanguageLevel


class ProfileOut(OutputSchema):
    display_name: str | None
    desired_roles: list[str]
    location: ProfileLocationOut | None
    salary: ProfileSalaryOut | None
    work_formats: list[WorkFormat]
    employment_types: list[EmploymentType]
    experience_level: ExperienceLevel | None
    languages: list[LanguageOut]
    relocation_ready: bool
    summary: str | None
    updated_at: datetime | None = Field(description="`null` until the profile is first saved.")


# --- skills ---------------------------------------------------------------------------------


class SkillOut(OutputSchema):
    id: uuid.UUID
    name: str


class SkillListOut(OutputSchema):
    items: list[SkillOut]


class SkillSearchParams(InputSchema):
    q: str = Field(min_length=1, max_length=50, description="Start of a skill name or alias.")
    limit: int = Field(default=10, ge=1, le=20)


class UserSkillIn(InputSchema):
    name: str = Field(
        min_length=1, max_length=100, description="Skill name or alias from the dictionary."
    )
    level: SkillLevel | None = None
    years_experience: Decimal | None = Field(
        default=None, ge=0, le=70, max_digits=3, decimal_places=1
    )


class UserSkillsIn(InputSchema):
    """The complete skill list: it replaces the stored one."""

    skills: list[UserSkillIn] = Field(max_length=MAX_PROFILE_SKILLS)


class UserSkillOut(OutputSchema):
    skill: SkillOut
    level: SkillLevel | None
    years_experience: Decimal | None


class UserSkillsOut(OutputSchema):
    items: list[UserSkillOut]
