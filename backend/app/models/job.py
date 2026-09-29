from __future__ import annotations

import uuid
from datetime import datetime
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

from app.core.enums import ACTIVE_SEARCH_JOB_STATUSES, SearchJobSourceStatus, SearchJobStatus
from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import StrEnumType, enum_check, non_negative_check

if TYPE_CHECKING:
    from app.models.search import Search
    from app.models.source import Source
    from app.models.vacancy import Vacancy

_ACTIVE_STATUSES_SQL = text(
    "status IN (" + ", ".join(f"'{status.value}'" for status in ACTIVE_SEARCH_JOB_STATUSES) + ")"
)


class SearchJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One asynchronous run of a search (SEARCH-006, TD-09, TD-12)."""

    __tablename__ = "search_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        Index("ix_search_jobs_search_id_created_at", "search_id", text("created_at DESC")),
        # Runs per user per hour (TD-12).
        Index("ix_search_jobs_user_id_created_at", "user_id", "created_at"),
        # One active run per search, even under concurrent requests (TD-12).
        Index(
            "uq_search_jobs_active_search_id",
            "search_id",
            unique=True,
            postgresql_where=_ACTIVE_STATUSES_SQL,
        ),
        # Serves the per-user active-job limit and the stuck-job sweeper.
        Index("ix_search_jobs_active_user_id", "user_id", postgresql_where=_ACTIVE_STATUSES_SQL),
        Index(
            "ix_search_jobs_active_deadline_at",
            "deadline_at",
            postgresql_where=_ACTIVE_STATUSES_SQL,
        ),
        enum_check("status", SearchJobStatus),
        non_negative_check("result_count"),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
    )

    search_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[SearchJobStatus] = mapped_column(
        StrEnumType(SearchJobStatus), server_default=SearchJobStatus.PENDING.value
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, Any]]
    progress: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    deadline_at: Mapped[datetime]
    cancel_requested_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    result_count: Mapped[int] = mapped_column(server_default=text("0"))

    search: Mapped[Search] = relationship(back_populates="jobs", lazy="raise")
    source_runs: Mapped[list[SearchJobSource]] = relationship(
        back_populates="search_job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    results: Mapped[list[SearchJobResult]] = relationship(
        back_populates="search_job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class SearchJobSource(TimestampMixin, Base):
    """Per-source outcome of a job; drives completed vs partially_completed (AMB-23)."""

    __tablename__ = "search_job_sources"
    __table_args__ = (
        Index(None, "source_id"),
        enum_check("status", SearchJobSourceStatus),
        non_negative_check("fetched_count"),
        non_negative_check("stored_count"),
        non_negative_check("duration_ms"),
        non_negative_check("attempts"),
    )

    search_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[SearchJobSourceStatus] = mapped_column(
        StrEnumType(SearchJobSourceStatus), server_default=SearchJobSourceStatus.PENDING.value
    )
    fetched_count: Mapped[int] = mapped_column(server_default=text("0"))
    stored_count: Mapped[int] = mapped_column(server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None]
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # {filter: {"mode": source|local|unsupported, "excluded": n, "unverified": n}}
    filter_report: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))

    search_job: Mapped[SearchJob] = relationship(back_populates="source_runs", lazy="raise")
    source: Mapped[Source] = relationship(lazy="raise")


class SearchJobResult(CreatedAtMixin, Base):
    """Vacancies returned by a job, capped at 200 per job (TD-13)."""

    __tablename__ = "search_job_results"
    __table_args__ = (
        Index(None, "search_job_id", "rank"),
        Index(None, "vacancy_id"),
        CheckConstraint("rank > 0", name="rank_positive"),
        CheckConstraint("match_score >= 0 AND match_score <= 1", name="match_score_range"),
    )

    search_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), primary_key=True
    )
    rank: Mapped[int]
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    # Requested filters this vacancy could not be checked against (never silently "matching").
    unverified_filters: Mapped[list[str]] = mapped_column(server_default=text("'[]'::jsonb"))

    search_job: Mapped[SearchJob] = relationship(back_populates="results", lazy="raise")
    vacancy: Mapped[Vacancy] = relationship(lazy="raise")
