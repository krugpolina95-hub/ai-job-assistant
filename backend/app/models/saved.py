from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.vacancy import Vacancy


class SavedVacancy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "saved_vacancies"
    __table_args__ = (
        UniqueConstraint("user_id", "vacancy_id"),
        Index("ix_saved_vacancies_user_id_created_at", "user_id", text("created_at DESC")),
        Index(None, "vacancy_id"),
        CheckConstraint("char_length(note) <= 2000", name="note_length"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # RESTRICT: retention must never delete a vacancy that someone saved (TD-24, GAP-21).
    vacancy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="RESTRICT"))
    note: Mapped[str | None] = mapped_column(Text)

    vacancy: Mapped[Vacancy] = relationship(lazy="raise")
