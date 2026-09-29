"""Sends email after the request has been answered.

Delivery time must not leak into responses: a password reset request for an existing account
would otherwise take longer than one for an unknown email (account enumeration). Failures are
logged without the address; the user can simply ask again.
"""

import asyncio

import structlog

from app.integrations.email.base import EmailSender, OutgoingEmail

logger = structlog.stdlib.get_logger(__name__)


class BackgroundEmailSender:
    def __init__(self, inner: EmailSender) -> None:
        self._inner = inner
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, email: OutgoingEmail) -> None:
        task = asyncio.create_task(self._deliver(email), name="send-email")
        self._tasks.add(task)  # keep a reference until done (asyncio holds only weak ones)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Waits for queued emails (shutdown, tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _deliver(self, email: OutgoingEmail) -> None:
        try:
            await self._inner.send(email)
        except Exception as exc:
            logger.error("email_send_failed", error=type(exc).__name__)
