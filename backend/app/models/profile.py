from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ExperienceLevel, LocationSource, SalaryPeriod
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import (
    StrEnumType,
    country_check,
    currency_check,
    enum_check,
    non_negative_check,
    range_check,
)

if TYPE_CHECKING:
    from app.models.user import User

# Consent defaults (AMB-09): the profile may feed the AI assistant, search history may not.
DEFAULT_AI_USE_PROFILE = True
DEFAULT_AI_USE_HISTORY = False


class UserProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Professional profile for AI context and matching. No sensitive attributes (MATCH-004)."""

    __tablename__ = "user_profiles"
    __table_args__ = (
        country_check("country"),
        currency_check("salary_currency"),
        non_negative_check("salary_min"),
        range_check("salary_min", "salary_max"),
        enum_check("salary_period", SalaryPeriod),
        enum_check("experience_level", ExperienceLevel),
        enum_check("location_source", LocationSource),
        CheckConstraint("char_length(summary) <= 5000", name="summary_length"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    desired_roles: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    country: Mapped[str | None] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    location_source: Mapped[LocationSource | None] = mapped_column(StrEnumType(LocationSource))
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[SalaryPeriod | None] = mapped_column(StrEnumType(SalaryPeriod))
    work_formats: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    employment_types: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    experience_level: Mapped[ExperienceLevel | None] = mapped_column(StrEnumType(ExperienceLevel))
    languages: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'::jsonb"))
    relocation_ready: Mapped[bool] = mapped_column(server_default=text("false"))
    summary: Mapped[str | None] = mapped_column(Text)
    ai_use_profile: Mapped[bool] = mapped_column(
        server_default=text(str(DEFAULT_AI_USE_PROFILE).lower())
    )
    ai_use_history: Mapped[bool] = mapped_column(
        server_default=text(str(DEFAULT_AI_USE_HISTORY).lower())
    )

    user: Mapped[User] = relationship(back_populates="profile", lazy="raise")
