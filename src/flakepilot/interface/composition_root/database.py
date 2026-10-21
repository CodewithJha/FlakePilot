"""The process's PostgreSQL engine (ADR-005), owned by the composition root.

Ownership: one application-scoped SQLAlchemy `AsyncEngine` (asyncpg driver) per process.
The composition root creates it at lifespan startup, checks it, and disposes it at shutdown.
Nothing creates engines or connections at import time or per request. Sessions are opened
only through `transaction()`. Pooled connections are pinged on checkout (`pool_pre_ping`),
so connections the server dropped (e.g. after a restart) are replaced instead of failing.

Only infrastructure layers and the composition root may import SQLAlchemy or Alembic;
only context infrastructure layers may import asyncpg directly (import-linter).
Driver and network errors can carry connection details or SQL, so they are logged by
exception class name only, and bound parameters are hidden from messages.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from flakepilot.interface.http.request_context import error_body, request_id_var

# Local-development defaults, not SLOs (RELIABILITY-AND-SLOS: TBD by benchmark).
POOL_SIZE = 10  # connections; no overflow, so this is the hard limit
POOL_TIMEOUT = 5.0  # seconds to wait for a free pooled connection
CONNECT_TIMEOUT = (
    5.0  # seconds to establish a connection; also bounds the startup check
)
STATEMENT_TIMEOUT_MS = 30_000  # PostgreSQL cancels any single statement after this
IDLE_IN_TRANSACTION_TIMEOUT_MS = (
    60_000  # PostgreSQL ends sessions idle inside a transaction
)
COMMAND_TIMEOUT = (
    35.0  # client-side backstop if the server cannot cancel (e.g. partition)
)
READINESS_TIMEOUT = 2.0  # seconds for the whole readiness check, whatever the above say
CLOSE_TIMEOUT = 5.0  # seconds to dispose gracefully before abandoning connections

EngineFactory = Callable[[str], AsyncEngine]

_logger = logging.getLogger("flakepilot.database")


def create_engine(dsn: str, **options: object) -> AsyncEngine:
    """Create the engine without connecting. `options` override engine arguments (tests)."""
    url = make_url(dsn).set(drivername="postgresql+asyncpg")
    connect_args = {
        "timeout": CONNECT_TIMEOUT,
        "command_timeout": COMMAND_TIMEOUT,
        "server_settings": {
            "statement_timeout": str(STATEMENT_TIMEOUT_MS),
            "idle_in_transaction_session_timeout": str(IDLE_IN_TRANSACTION_TIMEOUT_MS),
        },
    }
    arguments: dict[str, object] = {
        "pool_size": POOL_SIZE,
        "max_overflow": 0,
        "pool_timeout": POOL_TIMEOUT,
        "pool_pre_ping": True,
        "hide_parameters": True,
        "connect_args": connect_args,
    }
    return create_async_engine(url, **{**arguments, **options})


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One unit of work: a session, one transaction, one commit.

    Commits once when the block exits normally; rolls back on any exception, including
    cancellation and timeouts; always closes the session and returns its connection.

    Contract for code that uses it:
    - Application use cases own transaction boundaries: one `transaction()` per use case.
    - Repositories receive the session; they never commit or roll back
      (tests/architecture enforces no `.commit()`/`.rollback()` calls in src).
    - Nothing retries non-idempotent work automatically after a failed commit.
    - Tenant-scoped repositories must apply the org/repo tenant guard (ADR-005 decision 5);
      that guard is added with the first tenant-scoped repository.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        yield session


async def ping(engine: AsyncEngine, timeout: float) -> None:
    async with asyncio.timeout(timeout), engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def dispose_engine(engine: AsyncEngine, timeout: float = CLOSE_TIMEOUT) -> None:
    try:
        async with asyncio.timeout(timeout):
            await engine.dispose()
    except TimeoutError:
        _logger.warning("database dispose timed out; abandoning pooled connections")
        await engine.dispose(close=False)


class ReadinessCheck:
    """`await check()` is True when the database answers within the timeout.

    Single-flight: concurrent callers share one in-flight check, so readiness traffic
    holds at most one pooled connection. The timeout bounds pool waits too.
    """

    def __init__(self, engine: AsyncEngine, timeout: float = READINESS_TIMEOUT) -> None:
        self._engine = engine
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
            await ping(self._engine, self._timeout)
        except Exception as exc:  # noqa: BLE001 - any driver/network failure means not ready
            _logger.warning("database check failed: %s", type(exc).__name__)
            return False
        return True


async def _database_error(request: Request, exc: Exception) -> JSONResponse:
    """Database and network errors in requests: generic 500, class name only in the log.

    SQLAlchemy wraps driver and server errors (DBAPIError and subclasses); socket, DNS and
    connect-timeout failures surface unwrapped as OSError subclasses. Their messages can
    carry SQL, hosts or ports, so neither the message nor the traceback is logged.
    """
    kind = "database error" if isinstance(exc, SQLAlchemyError) else "connection error"
    _logger.error("%s: %s", kind, type(exc).__name__)
    body = error_body("internal_error", "Internal server error", request_id_var.get())
    return JSONResponse(body, 500)


def register_database_error_handler(app: FastAPI) -> None:
    app.add_exception_handler(SQLAlchemyError, _database_error)
    app.add_exception_handler(OSError, _database_error)
