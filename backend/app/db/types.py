from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Dialect, Text
from sqlalchemy.types import TypeDecorator


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """Stores a StrEnum as `text`; values are guarded in the DB by `enum_check`."""

    impl = Text
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        super().__init__()
        self.enum_cls = enum_cls

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self.enum_cls(value).value

    def process_result_value(self, value: Any, dialect: Dialect) -> E | None:
        return None if value is None else self.enum_cls(value)


def enum_check(column: str, enum_cls: type[StrEnum]) -> CheckConstraint:
    # Values come from code-defined enums, never from input, so inlining them is safe.
    allowed = ", ".join(f"'{member.value}'" for member in enum_cls)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


def non_negative_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} >= 0", name=f"{column}_non_negative")


def range_check(low: str, high: str) -> CheckConstraint:
    return CheckConstraint(
        f"{low} IS NULL OR {high} IS NULL OR {low} <= {high}", name=f"{low}_le_{high}"
    )


def currency_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f"{column}_iso4217")


def country_check(column: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '^[A-Z]{{2}}$'", name=f"{column}_iso3166")
