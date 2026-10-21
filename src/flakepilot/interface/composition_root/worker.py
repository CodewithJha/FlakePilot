"""Process entry for the leased worker. The API process does not start it.

    python -m flakepilot.interface.composition_root worker

No product job types are registered yet: this process is the runtime, and handlers
are the `handlers` argument, never a payload field. Cleanup jobs stay deferred.
"""

import asyncio
import logging
import signal
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.interface.composition_root import configure_logging
from flakepilot.interface.composition_root.database import (
    CONNECT_TIMEOUT,
    create_engine,
    dispose_engine,
    ping,
    transaction,
)
from flakepilot.interface.composition_root.settings import Settings
from flakepilot.orchestration.application.jobs import Clock, JobStore
from flakepilot.orchestration.application.worker import Handler, Jobs, Worker
from flakepilot.orchestration.infrastructure.store import SqlJobStore

_logger = logging.getLogger("flakepilot.worker")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def job_scope(engine: AsyncEngine, allowed: frozenset[str]) -> Jobs:
    @asynccontextmanager
    async def open_jobs() -> AsyncIterator[JobStore]:
        async with transaction(engine) as session:
            yield SqlJobStore(session, allowed)

    return open_jobs


def build_worker(
    engine: AsyncEngine,
    settings: Settings,
    handlers: Mapping[str, Handler] | None = None,
    *,
    clock: Clock | None = None,
    owner: str | None = None,
) -> Worker:
    registered = {} if handlers is None else dict(handlers)
    return Worker(
        jobs=job_scope(engine, frozenset(registered)),
        handlers=registered,
        clock=clock or SystemClock(),
        owner=owner or str(uuid.uuid4()),
        lease=timedelta(seconds=settings.worker_lease_seconds),
        batch=settings.worker_batch_size,
        poll_seconds=float(settings.worker_poll_seconds),
    )


async def run_worker(settings: Settings) -> int:
    configure_logging(settings.log_level)
    try:
        engine = create_engine(settings.database_url.reveal())
    except Exception as exc:  # noqa: BLE001 - the URL must not be logged
        _logger.error("worker stopped: %s", type(exc).__name__)
        return 1
    try:
        try:
            await ping(engine, CONNECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - driver errors can carry the host
            _logger.error("worker stopped: %s", type(exc).__name__)
            return 1
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await build_worker(engine, settings).run(stop)
    finally:
        await dispose_engine(engine)
    return 0
