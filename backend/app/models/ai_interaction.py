import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AIInteractionStatus, AITask
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check, non_negative_check


class AIInteraction(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Metadata of one AI call (cost, latency, outcome). Never stores prompt or response text."""

    __tablename__ = "ai_interactions"
    __table_args__ = (
        Index("ix_ai_interactions_user_id_created_at", "user_id", text("created_at DESC")),
        Index(None, "search_id"),
        enum_check("task", AITask),
        enum_check("status", AIInteractionStatus),
        non_negative_check("input_tokens"),
        non_negative_check("output_tokens"),
        non_negative_check("latency_ms"),
        non_negative_check("cost_estimate_usd"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    search_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("searches.id", ondelete="SET NULL")
    )
    task: Mapped[AITask] = mapped_column(StrEnumType(AITask))
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AIInteractionStatus] = mapped_column(StrEnumType(AIInteractionStatus))
    error_code: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    cost_estimate_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
