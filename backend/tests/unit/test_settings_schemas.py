from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.settings import SettingsUpdate


def test_partial_update_remembers_which_fields_were_sent() -> None:
    update = SettingsUpdate(ai_use_history=True)

    assert update.model_fields_set == {"ai_use_history"}
    assert update.preferred_locale is None


def test_valid_values() -> None:
    update = SettingsUpdate(
        preferred_locale="ru", timezone="Europe/Vilnius", ai_use_profile=False, ai_use_history=True
    )

    assert (update.preferred_locale, update.timezone) == ("ru", "Europe/Vilnius")
    assert (update.ai_use_profile, update.ai_use_history) == (False, True)


@pytest.mark.parametrize(
    ("body", "field", "error_type"),
    [
        ({"preferred_locale": "de"}, "preferred_locale", "enum"),
        ({"timezone": "Mars/Olympus_Mons"}, "timezone", "value_error"),
        ({"timezone": "x" * 65}, "timezone", "string_too_long"),
        ({"ai_use_profile": "maybe"}, "ai_use_profile", "bool_parsing"),
        ({"preferred_locale": None}, "preferred_locale", "value_error"),
        ({"timezone": None}, "timezone", "value_error"),
        ({"ai_use_profile": None}, "ai_use_profile", "value_error"),
        ({"ai_use_history": None}, "ai_use_history", "value_error"),
        ({"user_id": "00000000-0000-0000-0000-000000000000"}, "user_id", "extra_forbidden"),
        ({"email": "other@example.com"}, "email", "extra_forbidden"),
        ({"is_admin": True}, "is_admin", "extra_forbidden"),
        ({"display_name": "Ada"}, "display_name", "extra_forbidden"),
    ],
)
def test_invalid_settings_are_rejected(body: dict[str, Any], field: str, error_type: str) -> None:
    with pytest.raises(ValidationError) as error:
        SettingsUpdate(**body)

    [problem] = error.value.errors()
    assert problem["loc"][0] == field
    assert problem["type"] == error_type
