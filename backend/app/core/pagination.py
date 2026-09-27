"""Opaque cursors for keyset pagination (architecture §8.1: `?cursor=&limit=` → `next_cursor`).

A cursor is URL-safe base64 of a small JSON object. It is not signed: every query still
applies the caller's access filter, so a forged cursor can only skip within visible data.
"""

import base64
import binascii
import json
from typing import Any

from app.core.errors import ValidationFailedError

MAX_CURSOR_LENGTH = 512


class InvalidCursorError(ValidationFailedError):
    default_message = "Invalid pagination cursor"

    def __init__(self) -> None:
        super().__init__(details={"field": "cursor"})


def encode_cursor(position: dict[str, Any]) -> str:
    raw = json.dumps(position, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise InvalidCursorError()
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        position = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError() from exc
    if not isinstance(position, dict):
        raise InvalidCursorError()
    return position
