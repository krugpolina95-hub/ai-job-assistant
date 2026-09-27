from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import SourceType, StoragePolicy
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check


class Source(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Job source registry (TD-08, TD-23). Sources are disabled, never deleted."""

    __tablename__ = "sources"
    __table_args__ = (
        enum_check("source_type", SourceType),
        enum_check("storage_policy", StoragePolicy),
        CheckConstraint("retention_days > 0", name="retention_days_positive"),
        # Compliance gate (CLAUDE.md §7): a source can only run after its review is recorded.
        CheckConstraint(
            "NOT is_enabled OR compliance_reviewed_at IS NOT NULL", name="enabled_requires_review"
        ),
    )

    adapter_key: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[SourceType] = mapped_column(StrEnumType(SourceType))
    is_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    terms_url: Mapped[str | None] = mapped_column(Text)
    rate_limit: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    storage_policy: Mapped[StoragePolicy] = mapped_column(
        StrEnumType(StoragePolicy), server_default=StoragePolicy.NONE.value
    )
    retention_days: Mapped[int | None]
    attribution_text: Mapped[str | None] = mapped_column(Text)
    compliance_reviewed_at: Mapped[datetime | None]
    compliance_notes: Mapped[str | None] = mapped_column(Text)
