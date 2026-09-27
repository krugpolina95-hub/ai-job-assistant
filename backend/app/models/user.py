from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Text, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import non_negative_check

if TYPE_CHECKING:
    from app.models.profile import UserProfile
    from app.models.skill import UserSkill


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("char_length(email) <= 254", name="email_length"),
        non_negative_check("failed_login_count"),
    )

    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_verified: Mapped[bool] = mapped_column(server_default=text("false"))
    is_admin: Mapped[bool] = mapped_column(server_default=text("false"))
    preferred_locale: Mapped[str] = mapped_column(Text, server_default="en")
    timezone: Mapped[str] = mapped_column(Text, server_default="UTC")
    terms_version: Mapped[str | None] = mapped_column(Text)
    privacy_version: Mapped[str | None] = mapped_column(Text)
    terms_accepted_at: Mapped[datetime | None]
    failed_login_count: Mapped[int] = mapped_column(server_default=text("0"))
    locked_until: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]

    profile: Mapped[UserProfile | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
    skills: Mapped[list[UserSkill]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
