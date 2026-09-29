import smtplib
from typing import Any, ClassVar

import pytest
import structlog

from app.core.config import Settings
from app.integrations.email.background import BackgroundEmailSender
from app.integrations.email.base import OutgoingEmail
from app.integrations.email.smtp import SmtpEmailSender
from app.integrations.email.templates import password_reset_email
from tests.conftest import build_test_settings
from tests.fakes import RecordingEmailSender

EMAIL = OutgoingEmail(to="user@example.com", subject="Hello", body="Body text\n")
LINK = "https://app.example.com/en/reset-password#token=abc"


@pytest.mark.parametrize(("locale", "word"), [("en", "password"), ("ru", "пароль")])
def test_password_reset_email_is_localized(locale: str, word: str) -> None:
    subject, body = password_reset_email(locale, link=LINK, minutes=30)

    assert word in body.lower()
    assert LINK in body
    assert "30" in body
    assert subject


def test_unknown_locale_falls_back_to_english() -> None:
    assert password_reset_email("de", link=LINK, minutes=30) == password_reset_email(
        "en", link=LINK, minutes=30
    )


async def test_background_sender_delivers_after_returning() -> None:
    inner = RecordingEmailSender()
    sender = BackgroundEmailSender(inner)

    await sender.send(EMAIL)
    await sender.drain()

    assert inner.sent == [EMAIL]


async def test_background_sender_logs_failures_without_the_address() -> None:
    class BrokenSender:
        async def send(self, email: OutgoingEmail) -> None:
            raise smtplib.SMTPServerDisconnected(f"lost connection sending to {email.to}")

    sender = BackgroundEmailSender(BrokenSender())
    with structlog.testing.capture_logs() as logs:
        await sender.send(EMAIL)
        await sender.drain()

    assert [entry["event"] for entry in logs] == ["email_send_failed"]
    assert EMAIL.to not in str(logs)


class FakeSmtp:
    instances: ClassVar[list["FakeSmtp"]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.address = (host, port)
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.messages: list[Any] = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, **kwargs: Any) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message: Any) -> None:
        self.calls.append("send")
        self.messages.append(message)


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSmtp]:
    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    return FakeSmtp


def smtp_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "email_from": "AI Job Assistant <no-reply@example.com>",
    }
    return build_test_settings(**(values | overrides))


async def test_smtp_sender_builds_a_plain_text_message(fake_smtp: type[FakeSmtp]) -> None:
    await SmtpEmailSender(smtp_settings()).send(EMAIL)

    (smtp,) = fake_smtp.instances
    assert smtp.address == ("smtp.example.com", 587)
    assert smtp.calls == ["send", "quit"]
    message = smtp.messages[0]
    assert message["From"] == "AI Job Assistant <no-reply@example.com>"
    assert message["To"] == EMAIL.to
    assert message["Subject"] == EMAIL.subject
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == EMAIL.body


async def test_smtp_sender_uses_starttls_and_credentials(fake_smtp: type[FakeSmtp]) -> None:
    settings = smtp_settings(smtp_security="starttls", smtp_username="u", smtp_password="p")

    await SmtpEmailSender(settings).send(EMAIL)

    assert fake_smtp.instances[0].calls == ["starttls", "login:u:p", "send", "quit"]


async def test_smtp_sender_uses_implicit_tls(fake_smtp: type[FakeSmtp]) -> None:
    await SmtpEmailSender(smtp_settings(smtp_security="tls", smtp_port=465)).send(EMAIL)

    assert "context" in fake_smtp.instances[0].kwargs
