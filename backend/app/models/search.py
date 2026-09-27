from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import SearchChannel, SearchStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check

if TYPE_CHECKING:
    from app.models.job import SearchJob


class Search(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A saved set of search parameters (SearchParamsV1, TD-10). Each run is a SearchJob."""

    __tablename__ = "searches"
    __table_args__ = (
        Index("ix_searches_user_id_created_at", "user_id", text("created_at DESC")),
        enum_check("channel", SearchChannel),
        enum_check("status", SearchStatus),
        CheckConstraint("params_schema_version > 0", name="params_schema_version_positive"),
        CheckConstraint("char_length(original_text) <= 2000", name="original_text_length"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(Text)
    original_text: Mapped[str | None] = mapped_column(Text)
    normalized_params: Mapped[dict[str, Any]]
    params_schema_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    channel: Mapped[SearchChannel] = mapped_column(StrEnumType(SearchChannel))
    status: Mapped[SearchStatus] = mapped_column(
        StrEnumType(SearchStatus), server_default=SearchStatus.DRAFT.value
    )

    jobs: Mapped[list[SearchJob]] = relationship(
        back_populates="search", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
