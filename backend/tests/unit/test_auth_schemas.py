from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.auth import LoginIn, RegisterIn, UserOut

VALID: dict[str, Any] = {
    "email": "ada@example.com",
    "password": "long enough pw",
    "accept_terms": True,
}


def test_valid_registration_with_defaults() -> None:
    data = RegisterIn(**VALID)

    assert data.preferred_locale == "en"
    assert data.timezone == "UTC"


def test_password_is_kept_verbatim_and_hidden() -> None:
    data = RegisterIn(**VALID | {"password": "  spaces count  ", "email": "  Ada@Example.COM "})

    assert data.password.get_secret_value() == "  spaces count  "
    assert "spaces count" not in repr(data)
    assert str(data.email) == "Ada@example.com"


@pytest.mark.parametrize(
    ("override", "field", "error_type"),
    [
        ({"email": "not-an-email"}, "email", "value_error"),
        ({"email": f"{'a' * 250}@example.com"}, "email", "value_error"),
        ({"password": "nine char"}, "password", "too_short"),
        ({"password": "x" * 129}, "password", "too_long"),
        ({"password": "ADA@example.com"}, "password", "value_error"),
        ({"accept_terms": False}, "accept_terms", "literal_error"),
        ({"preferred_locale": "de"}, "preferred_locale", "enum"),
        ({"timezone": "Mars/Olympus_Mons"}, "timezone", "value_error"),
        ({"is_admin": True}, "is_admin", "extra_forbidden"),
    ],
)
def test_registration_rejects_invalid_input(
    override: dict[str, Any], field: str, error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        RegisterIn(**VALID | override)

    [problem] = error.value.errors()
    assert problem["loc"] == (field,)
    assert problem["type"] == error_type


def test_registration_requires_consent_field() -> None:
    with pytest.raises(ValidationError):
        RegisterIn(email="ada@example.com", password="long enough pw")  # type: ignore[call-arg]


def test_login_accepts_short_password_and_defaults_to_browser_session() -> None:
    data = LoginIn(email="ada@example.com", password="x")

    assert data.remember_me is False


def test_user_out_exposes_no_secrets() -> None:
    assert set(UserOut.model_fields) == {
        "id",
        "email",
        "is_verified",
        "preferred_locale",
        "timezone",
        "created_at",
    }
