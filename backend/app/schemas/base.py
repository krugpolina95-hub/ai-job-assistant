from pydantic import BaseModel, ConfigDict


class InputSchema(BaseModel):
    """Base for request bodies: unknown fields are rejected, strings are trimmed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutputSchema(BaseModel):
    """Base for responses. Built from ORM objects in services, never returned as ORM objects."""

    model_config = ConfigDict(from_attributes=True)
