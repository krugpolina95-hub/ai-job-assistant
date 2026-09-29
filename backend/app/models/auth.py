from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AuthEventType
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check

if TYPE_CHECKING:
    from app.models.user import User


class AuthEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only log of security-relevant account events (AUTH-006). Holds no raw email or
    IP address: the email as a keyed hash, the IP as its network prefix."""

    __tablename__ = "auth_events"
    __table_args__ = (
        Index(None, "user_id", "created_at"),
        Index(None, "created_at"),
        enum_check("event_type", AuthEventType),
        CheckConstraint("char_length(email_hash) <= 64", name="email_hash_length"),
        CheckConstraint("char_length(ip_prefix) <= 64", name="ip_prefix_length"),
        CheckConstraint("char_length(user_agent) <= 512", name="user_agent_length"),
        CheckConstraint("char_length(detail) <= 64", name="detail_length"),
    )

    # NULL when the event concerns an email with no account (e.g. a failed login).
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_type: Mapped[AuthEventType] = mapped_column(StrEnumType(AuthEventType))
    email_hash: Mapped[str | None] = mapped_column(Text)
    ip_prefix: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    # Machine-readable reason, e.g. "wrong_password" or the rate limit that was hit.
    detail: Mapped[str | None] = mapped_column(Text)


class PasswordResetToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Single-use password reset token (AUTH-004). Only the SHA-256 of the emailed token is
    stored, like session tokens."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index(None, "user_id"),
        CheckConstraint("char_length(token_hash) = 64", name="token_hash_sha256"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]

    user: Mapped[User] = relationship(lazy="raise")
