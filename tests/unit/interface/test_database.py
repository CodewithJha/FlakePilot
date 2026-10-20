import asyncio
import time

from support import FakePool

from flakepilot.interface.composition_root.database import ReadinessCheck, close_pool


def test_readiness_check_gives_up_on_a_hanging_database() -> None:
    pool = FakePool()
    pool.hang = True
    started = time.monotonic()
    assert asyncio.run(ReadinessCheck(pool, timeout=0.1)()) is False
    assert time.monotonic() - started < 1.0


def test_concurrent_readiness_checks_share_one_probe() -> None:
    pool = FakePool()
    pool.delay = 0.05
    check = ReadinessCheck(pool)

    async def scenario() -> list[bool]:
        results = await asyncio.gather(*(check() for _ in range(20)))
        results.append(await check())
        return results

    assert asyncio.run(scenario()) == [True] * 21
    assert pool.acquires == 2  # one shared probe, then a fresh one: no caching


def test_close_terminates_when_graceful_close_hangs() -> None:
    pool = FakePool()
    pool.hang_on_close = True
    started = time.monotonic()
    asyncio.run(close_pool(pool, timeout=0.1))
    assert pool.terminated and not pool.closed
    assert time.monotonic() - started < 1.0
