from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import EmploymentType, ExperienceLevel, SalaryPeriod, WorkFormat
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
    from app.models.source import Source


class Vacancy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A job posting from one source. Shared data: never owned by a user.

    Unknown source data stays NULL (VACANCY-003); nothing is inferred.
    """

    __tablename__ = "vacancies"
    __table_args__ = (
        Index(
            "uq_vacancies_source_id_external_id",
            "source_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(None, "source_id"),
        Index(None, "canonical_url"),
        Index(None, "content_hash"),
        Index(None, "duplicate_group_id"),
        Index(
            "ix_vacancies_normalized_title_trgm",
            "normalized_title",
            postgresql_using="gin",
            postgresql_ops={"normalized_title": "gin_trgm_ops"},
        ),
        Index("ix_vacancies_skills_gin", "skills", postgresql_using="gin"),
        country_check("country"),
        currency_check("salary_currency"),
        non_negative_check("salary_min"),
        range_check("salary_min", "salary_max"),
        range_check("salary_monthly_min_eur", "salary_monthly_max_eur"),
        enum_check("salary_period", SalaryPeriod),
        enum_check("remote_type", WorkFormat),
        enum_check("employment_type", EmploymentType),
        enum_check("experience_level", ExperienceLevel),
        CheckConstraint("url ~ '^https?://'", name="url_http"),
        CheckConstraint("canonical_url ~ '^https?://'", name="canonical_url_http"),
        CheckConstraint("char_length(content_hash) = 64", name="content_hash_sha256"),
        CheckConstraint(
            "char_length(description_snippet) <= 500", name="description_snippet_length"
        ),
        CheckConstraint("is_active = (removed_at IS NULL)", name="active_matches_removed_at"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)

    title: Mapped[str] = mapped_column(Text)
    normalized_title: Mapped[str] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    company_normalized: Mapped[str | None] = mapped_column(Text)

    description: Mapped[str | None] = mapped_column(Text)
    description_snippet: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    responsibilities: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    skills: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    language: Mapped[str | None] = mapped_column(Text)

    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[SalaryPeriod | None] = mapped_column(StrEnumType(SalaryPeriod))
    salary_raw: Mapped[str | None] = mapped_column(Text)
    salary_monthly_min_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_monthly_max_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    country: Mapped[str | None] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    location_raw: Mapped[str | None] = mapped_column(Text)
    remote_type: Mapped[WorkFormat | None] = mapped_column(StrEnumType(WorkFormat))
    employment_type: Mapped[EmploymentType | None] = mapped_column(StrEnumType(EmploymentType))
    experience_level: Mapped[ExperienceLevel | None] = mapped_column(StrEnumType(ExperienceLevel))

    published_at: Mapped[datetime | None]
    collected_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_verified_at: Mapped[datetime | None]
    content_hash: Mapped[str] = mapped_column(Text)
    duplicate_group_id: Mapped[uuid.UUID | None]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    removed_at: Mapped[datetime | None]

    source: Mapped[Source] = relationship(lazy="raise")
