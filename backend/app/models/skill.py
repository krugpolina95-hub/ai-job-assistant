from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import SkillLevel
from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check

if TYPE_CHECKING:
    from app.models.user import User


class Skill(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Curated skill dictionary (GAP-14). Shared data, not user-owned."""

    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text, unique=True)

    aliases: Mapped[list[SkillAlias]] = relationship(
        back_populates="skill", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )


class SkillAlias(CreatedAtMixin, Base):
    """Normalized synonym (e.g. "js" → JavaScript), optionally locale-specific."""

    __tablename__ = "skill_aliases"
    __table_args__ = (Index(None, "skill_id"),)

    alias_normalized: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"))
    locale: Mapped[str | None] = mapped_column(Text)

    skill: Mapped[Skill] = relationship(back_populates="aliases", lazy="raise")


class UserSkill(TimestampMixin, Base):
    __tablename__ = "user_skills"
    __table_args__ = (
        Index(None, "skill_id"),
        enum_check("skill_level", SkillLevel),
        CheckConstraint(
            "years_experience >= 0 AND years_experience <= 70", name="years_experience_range"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
    )
    skill_level: Mapped[SkillLevel | None] = mapped_column(StrEnumType(SkillLevel))
    years_experience: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))

    user: Mapped[User] = relationship(back_populates="skills", lazy="raise")
    skill: Mapped[Skill] = relationship(lazy="raise")
