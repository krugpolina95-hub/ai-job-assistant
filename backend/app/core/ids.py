import os
import time
import uuid

_RAND_A_BITS = 12
_RAND_B_BITS = 62


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 version 7) for index locality (TD-26)."""
    timestamp_ms = time.time_ns() // 1_000_000
    random_bits = int.from_bytes(os.urandom(10), "big")
    rand_a = (random_bits >> _RAND_B_BITS) & ((1 << _RAND_A_BITS) - 1)
    rand_b = random_bits & ((1 << _RAND_B_BITS) - 1)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)
