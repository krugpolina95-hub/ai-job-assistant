from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ExplanationSource, MatchLevel
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check

if TYPE_CHECKING:
    from app.models.vacancy import Vacancy


class VacancyMatch(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Deterministic match of a vacancy for one job run (TD-11). `match_score` is internal only."""

    __tablename__ = "vacancy_matches"
    __table_args__ = (
        UniqueConstraint("search_job_id", "vacancy_id"),
        # Latest match for a vacancy per user (compare, AMB-17).
        Index(
            "ix_vacancy_matches_user_id_vacancy_id_created_at",
            "user_id",
            "vacancy_id",
            text("created_at DESC"),
        ),
        Index(None, "vacancy_id"),
        enum_check("match_level", MatchLevel),
        enum_check("explanation_source", ExplanationSource),
        CheckConstraint("match_score >= 0 AND match_score <= 1", name="match_score_range"),
        CheckConstraint("algorithm_version > 0", name="algorithm_version_positive"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    search_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE")
    )
    vacancy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"))
    match_level: Mapped[MatchLevel] = mapped_column(StrEnumType(MatchLevel))
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    factors: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'::jsonb"))
    matching_skills: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    missing_skills: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))
    explanation: Mapped[str | None] = mapped_column(Text)
    explanation_locale: Mapped[str | None] = mapped_column(Text)
    explanation_source: Mapped[ExplanationSource | None] = mapped_column(
        StrEnumType(ExplanationSource)
    )
    algorithm_version: Mapped[int] = mapped_column(SmallInteger)

    vacancy: Mapped[Vacancy] = relationship(lazy="raise")
