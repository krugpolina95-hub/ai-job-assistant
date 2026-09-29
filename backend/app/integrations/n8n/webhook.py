"""Backend → n8n trigger (TD-09, architecture §6.1): a signed POST to the WF-001 webhook.

The body is `{"job_id", "request_id"}`: n8n fetches everything else from the internal API. It
is signed with N8N_WEBHOOK_SECRET (not the secret n8n uses towards the backend), with the same
scheme as internal calls (`app/core/security.py:request_signature`), so the WF-001 Code node
verifies `X-Timestamp` and `X-Signature` before doing anything.
"""

import json
import time
import uuid
from urllib.parse import urlsplit

import httpx

from app.core.security import request_signature


class N8nTriggerError(Exception):
    """The trigger didn't reach n8n or n8n refused it. No URL or secret in the message."""


class N8nWebhookClient:
    def __init__(self, http: httpx.AsyncClient, *, url: str, secret: str) -> None:
        self._http = http
        self._url = url
        self._path = urlsplit(url).path or "/"
        self._secret = secret

    async def trigger(self, job_id: uuid.UUID, request_id: str | None) -> None:
        body = json.dumps({"job_id": str(job_id), "request_id": request_id}).encode()
        timestamp = str(int(time.time()))
        signature = request_signature(
            self._secret, timestamp=timestamp, method="POST", path=self._path, body=body
        )
        try:
            response = await self._http.post(
                self._url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Timestamp": timestamp,
                    "X-Signature": signature,
                },
            )
        except httpx.HTTPError as exc:
            raise N8nTriggerError(f"n8n unreachable ({type(exc).__name__})") from None
        if not response.is_success:
            raise N8nTriggerError(f"n8n answered HTTP {response.status_code}")
