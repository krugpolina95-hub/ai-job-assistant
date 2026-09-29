"""Password hashing (TD-04) and opaque tokens (TD-03). No FastAPI imports: services use this."""

import asyncio
import hashlib
import hmac
import secrets

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

TOKEN_BYTES = 32  # 256 bits of entropy


class PasswordHasher:
    """Argon2id. Hashing is CPU-bound, so it runs in a worker thread to keep the event loop
    responsive (argon2-cffi releases the GIL)."""

    def __init__(self, *, time_cost: int, memory_kib: int, parallelism: int) -> None:
        self._hash = PasswordHash(
            (Argon2Hasher(time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism),)
        )
        # Verified against when the email is unknown, so both login failures cost the same time.
        self._dummy_hash = self._hash.hash(secrets.token_urlsafe(16))

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hash.hash, password)

    async def verify(self, password: str, password_hash: str) -> tuple[bool, str | None]:
        """Returns (valid, new_hash). `new_hash` is set when the stored hash uses outdated
        parameters and should be replaced (rehash on login)."""
        return await asyncio.to_thread(self._hash.verify_and_update, password, password_hash)

    async def verify_dummy(self, password: str) -> None:
        await asyncio.to_thread(self._hash.verify, password, self._dummy_hash)


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Tokens are stored only as SHA-256: a database leak does not leak usable sessions.
    A fast hash is enough because the token itself has 256 bits of entropy."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode(), provided.encode())


def hash_identifier(value: str, key: str) -> str:
    """Stable, non-reversible identifier for logs (TD-21)."""
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()[:16]


SIGNATURE_PREFIX = "sha256="


def request_signature(secret: str, *, timestamp: str, method: str, path: str, body: bytes) -> str:
    """HMAC for calls between the backend and n8n (architecture §8.3), both directions.

    The signed text binds the time, the method, the path (with query) and the body hash, one
    per line. Binding method and path matters because many internal calls have an empty body:
    a signature can't be replayed against another endpoint or another job."""
    canonical = "\n".join(
        [timestamp, method.upper(), path, hashlib.sha256(body).hexdigest()]
    ).encode()
    return SIGNATURE_PREFIX + hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def signature_is_valid(
    secret: str,
    signature: str | None,
    *,
    timestamp: str | None,
    method: str,
    path: str,
    body: bytes,
    now: float,
    max_age_seconds: int,
) -> bool:
    """A matching signature on a timestamp within ±`max_age_seconds` of `now` (replay bound)."""
    if not signature or not timestamp or not timestamp.isdigit():
        return False
    if abs(now - int(timestamp)) > max_age_seconds:
        return False
    expected = request_signature(secret, timestamp=timestamp, method=method, path=path, body=body)
    return hmac.compare_digest(expected.encode(), signature.encode())
