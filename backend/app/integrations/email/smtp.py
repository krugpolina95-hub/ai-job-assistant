"""SMTP delivery (TD-27): Mailpit locally, the chosen provider's SMTP endpoint when deployed.

The standard library client runs in a worker thread, so no extra dependency is needed and the
event loop never blocks on the network.
"""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import Settings
from app.integrations.email.base import OutgoingEmail


class SmtpEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send(self, email: OutgoingEmail) -> None:
        await asyncio.to_thread(self._send, self._build(email))

    def _build(self, email: OutgoingEmail) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._settings.email_from
        message["To"] = email.to
        message["Subject"] = email.subject
        message.set_content(email.body)
        return message

    def _send(self, message: EmailMessage) -> None:
        s = self._settings
        smtp: smtplib.SMTP
        if s.smtp_security == "tls":
            smtp = smtplib.SMTP_SSL(
                s.smtp_host,
                s.smtp_port,
                timeout=s.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
        with smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)
