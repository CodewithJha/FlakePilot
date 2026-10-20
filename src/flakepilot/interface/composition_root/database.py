"""The process's PostgreSQL connection pool (ADR-005), owned by the composition root.

Only infrastructure layers and the composition root may import asyncpg (import-linter).
Driver error messages can carry connection details, so failures are logged by exception
class name only.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

POOL_MAX_SIZE = 10
CONNECT_TIMEOUT = 5.0  # seconds: each new connection, and the startup check
READINESS_TIMEOUT = 2.0  # seconds: the whole readiness check (acquire + SELECT 1)
CLOSE_TIMEOUT = 5.0  # seconds: graceful close before connections are terminated

PoolFactory = Callable[[str], Awaitable[Any]]

_logger = logging.getLogger("flakepilot.database")


async def create_pool(dsn: str) -> Any:
    """Create the pool without connecting; connections open on first acquire."""
    return await asyncpg.create_pool(
        dsn, min_size=0, max_size=POOL_MAX_SIZE, timeout=CONNECT_TIMEOUT
    )


async def ping(pool: Any, timeout: float) -> None:
    async with asyncio.timeout(timeout):
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")


async def close_pool(pool: Any, timeout: float = CLOSE_TIMEOUT) -> None:
    try:
        async with asyncio.timeout(timeout):
            await pool.close()
    except TimeoutError:
        pool.terminate()


class ReadinessCheck:
    """`await check()` is True when the database answers within the timeout.

    Single-flight: concurrent callers share one in-flight check, so readiness traffic
    holds at most one pool connection.
    """

    def __init__(self, pool: Any, timeout: float = READINESS_TIMEOUT) -> None:
        self._pool = pool
        self._timeout = timeout
        self._inflight: asyncio.Task[bool] | None = None

    async def __call__(self) -> bool:
        if self._inflight is None:
            self._inflight = asyncio.create_task(self._check())
            self._inflight.add_done_callback(self._clear)
        return await asyncio.shield(self._inflight)

    def _clear(self, _: object) -> None:
        self._inflight = None

    async def _check(self) -> bool:
        try:
            await ping(self._pool, self._timeout)
        except Exception as exc:  # noqa: BLE001 - any driver/network failure means not ready
            _logger.warning("database check failed: %s", type(exc).__name__)
            return False
        return True
