from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class UserSession(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Server-side login session (TD-03). Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index(None, "user_id"),
        CheckConstraint("char_length(token_hash) = 64", name="token_hash_sha256"),
        CheckConstraint("char_length(user_agent) <= 512", name="user_agent_length"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    user_agent: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="raise")
