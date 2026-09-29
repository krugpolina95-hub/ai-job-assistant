import uuid
import zoneinfo
from datetime import datetime
from functools import cache
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
)

from app.core.enums import SupportedLocale
from app.schemas.base import InputSchema, OutputSchema

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128

Email = Annotated[EmailStr, Field(max_length=254)]
# SecretStr keeps passwords out of reprs, logs and error messages.
NewPassword = Annotated[
    SecretStr, Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
]
# Checked against the stored hash, so only bounded: older passwords may follow older rules.
CurrentPassword = Annotated[SecretStr, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]
# generate_token(): 32 random bytes, URL-safe base64 (43 characters).
ResetToken = Annotated[str, Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


@cache
def _known_timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def _known_timezone(value: str) -> str:
    if value not in _known_timezones():
        raise ValueError("Unknown time zone")
    return value


TimeZoneName = Annotated[
    str, Field(max_length=64, description="IANA time zone name."), AfterValidator(_known_timezone)
]


class _CredentialsIn(InputSchema):
    # Passwords are used exactly as typed: whitespace is significant, so no global stripping.
    model_config = ConfigDict(str_strip_whitespace=False)

    email: Email

    @field_validator("email", mode="before")
    @classmethod
    def strip_email(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RegisterIn(_CredentialsIn):
    password: NewPassword
    accept_terms: Literal[True] = Field(
        description="Consent to the current terms of use and privacy policy (AUTH-002)."
    )
    preferred_locale: SupportedLocale = SupportedLocale.EN
    timezone: TimeZoneName = "UTC"

    @field_validator("password")
    @classmethod
    def password_differs_from_email(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        email = info.data.get("email")
        if email and value.get_secret_value().casefold() == str(email).casefold():
            raise ValueError("Password must not be the email address")
        return value


class LoginIn(_CredentialsIn):
    password: CurrentPassword
    remember_me: bool = False


class _PasswordsIn(InputSchema):
    # Passwords are used exactly as typed: whitespace is significant.
    model_config = ConfigDict(str_strip_whitespace=False)


class ChangePasswordIn(_PasswordsIn):
    current_password: CurrentPassword
    new_password: NewPassword


class ForgotPasswordIn(_CredentialsIn):
    pass


class ResetPasswordIn(_PasswordsIn):
    token: ResetToken
    new_password: NewPassword


class UserOut(OutputSchema):
    id: uuid.UUID
    email: str
    is_verified: bool
    preferred_locale: SupportedLocale
    timezone: str
    created_at: datetime
