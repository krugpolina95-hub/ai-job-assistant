"""Static checks of schema conventions (CLAUDE.md §6 Database) that need no database."""

import pytest
from sqlalchemy import CheckConstraint, DateTime, Table, UniqueConstraint

import app.models  # noqa: F401
from app.db.base import Base
from app.db.types import StrEnumType

TABLES = list(Base.metadata.sorted_tables)


def _leading_indexed_columns(table: Table) -> set[str]:
    """First column of the PK, every index and every unique constraint: the columns a FK
    lookup (e.g. an ON DELETE CASCADE scan) can use an index for."""
    column_lists = [list(table.primary_key.columns)]
    column_lists += [list(index.columns) for index in table.indexes]
    column_lists += [list(c.columns) for c in table.constraints if isinstance(c, UniqueConstraint)]
    return {columns[0].name for columns in column_lists if columns}


def test_expected_tables_exist() -> None:
    assert {table.name for table in TABLES} == {
        "users",
        "user_profiles",
        "skills",
        "skill_aliases",
        "user_skills",
        "sources",
        "vacancies",
        "searches",
        "search_jobs",
        "search_job_sources",
        "search_job_results",
        "vacancy_matches",
        "saved_vacancies",
        "search_history_events",
        "ai_interactions",
    }


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_every_foreign_key_is_indexed(table: Table) -> None:
    leading = _leading_indexed_columns(table)
    for fk in table.foreign_keys:
        assert fk.parent.name in leading, f"{table.name}.{fk.parent.name} has no index"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_user_owned_rows_cascade_from_users(table: Table) -> None:
    for fk in table.foreign_keys:
        if fk.column.table.name == "users":
            assert fk.ondelete == "CASCADE", f"{table.name}.{fk.parent.name}"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_every_enum_column_has_a_check_constraint(table: Table) -> None:
    checks = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
    for column in table.columns:
        if isinstance(column.type, StrEnumType):
            assert f"ck_{table.name}_{column.name}" in checks, f"{table.name}.{column.name}"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_timestamps_are_timezone_aware(table: Table) -> None:
    for column in table.columns:
        if isinstance(column.type, DateTime):
            assert column.type.timezone, f"{table.name}.{column.name}"
    assert "created_at" in table.columns
