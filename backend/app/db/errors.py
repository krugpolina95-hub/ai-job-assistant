from sqlalchemy.exc import IntegrityError


def violated_constraint(error: IntegrityError) -> str | None:
    """Name of the Postgres constraint behind an IntegrityError (asyncpg driver)."""
    cause = error.orig.__cause__ if error.orig is not None else None
    constraint: str | None = getattr(cause, "constraint_name", None)
    return constraint
