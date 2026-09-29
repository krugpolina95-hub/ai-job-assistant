"""Outgoing email port (TD-27). Services build messages; senders only deliver them."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str  # plain text (TD-27: plain-text first)


class EmailSender(Protocol):
    async def send(self, email: OutgoingEmail) -> None: ...
