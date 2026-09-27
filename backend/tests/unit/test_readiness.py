import asyncio

from app.services.readiness import check_readiness


async def ok() -> None:
    return None


async def fails() -> None:
    raise ConnectionRefusedError


async def hangs() -> None:
    await asyncio.sleep(10)


async def test_all_checks_pass() -> None:
    assert await check_readiness({"database": ok, "redis": ok}, 0.1) == {
        "database": True,
        "redis": True,
    }


async def test_failing_check_is_reported_without_raising() -> None:
    assert await check_readiness({"database": fails, "redis": ok}, 0.1) == {
        "database": False,
        "redis": True,
    }


async def test_slow_check_times_out() -> None:
    assert await check_readiness({"database": hangs}, 0.05) == {"database": False}
