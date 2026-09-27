import asyncio
from collections.abc import Awaitable, Callable, Mapping

import structlog

logger = structlog.stdlib.get_logger(__name__)

ReadinessCheck = Callable[[], Awaitable[None]]


async def _run_check(name: str, check: ReadinessCheck, timeout_seconds: float) -> bool:
    try:
        await asyncio.wait_for(check(), timeout=timeout_seconds)
    except Exception as exc:
        # Any failure (refused connection, timeout, auth error) means "not ready".
        logger.warning("readiness_check_failed", check=name, error=type(exc).__name__)
        return False
    return True


async def check_readiness(
    checks: Mapping[str, ReadinessCheck], timeout_seconds: float
) -> dict[str, bool]:
    names = list(checks)
    results = await asyncio.gather(
        *(_run_check(name, checks[name], timeout_seconds) for name in names)
    )
    return dict(zip(names, results, strict=True))
