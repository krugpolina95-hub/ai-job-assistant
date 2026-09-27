import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import HistoryEventType
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check


class SearchHistoryEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "search_history_events"
    __table_args__ = (
        Index("ix_search_history_events_user_id_created_at", "user_id", text("created_at DESC")),
        Index(None, "search_id"),
        enum_check("event_type", HistoryEventType),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    search_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE")
    )
    event_type: Mapped[HistoryEventType] = mapped_column(StrEnumType(HistoryEventType))
    # `metadata` is reserved by SQLAlchemy's declarative API, so the attribute is renamed.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", server_default=text("'{}'::jsonb")
    )
