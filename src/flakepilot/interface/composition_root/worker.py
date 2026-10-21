"""Process entry for the leased worker. The API process does not start it.

    python -m flakepilot.interface.composition_root worker

The registered job type is browser reproduction. Its payload is an execution id.
Handlers are the `handlers` argument, never a payload field. Cleanup jobs stay deferred.
"""

import asyncio
import logging
import os
import signal
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.execution.application.plane import UnavailableRuntimeReaper
from flakepilot.integrations.infrastructure.sandbox import reap_workdirs
from flakepilot.interface.composition_root import configure_logging
from flakepilot.interface.composition_root.database import (
    CONNECT_TIMEOUT,
    create_engine,
    dispose_engine,
    ping,
    transaction,
)
from flakepilot.interface.composition_root.relay import deliver_batch
from flakepilot.interface.composition_root.reproduction import reproduction_handlers
from flakepilot.interface.composition_root.scheduling import enqueue_missing
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


def startup_cleanup(root: Path, *, now: float) -> tuple[int, str]:
    """Remove expired work directories. Runtime resources are not reaped.

    The second value is the reaper's reason. `runtime_reaper_unavailable`
    means no container or microVM was inspected or removed.
    """
    removed = reap_workdirs(root, now=now, ttl_seconds=3600)
    report = UnavailableRuntimeReaper().reclaim()
    return removed, report.reason


def build_worker(
    engine: AsyncEngine,
    settings: Settings,
    handlers: Mapping[str, Handler] | None = None,
    *,
    clock: Clock | None = None,
    owner: str | None = None,
) -> Worker:
    registered = {} if handlers is None else dict(handlers)

    async def deliver() -> None:
        await deliver_batch(engine, settings.worker_batch_size)

    return Worker(
        jobs=job_scope(engine, frozenset(registered)),
        handlers=registered,
        clock=clock or SystemClock(),
        owner=owner or str(uuid.uuid4()),
        lease=timedelta(seconds=settings.worker_lease_seconds),
        batch=settings.worker_batch_size,
        poll_seconds=float(settings.worker_poll_seconds),
        deliver=deliver,
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
        removed, reap_reason = startup_cleanup(
            Path(os.environ.get("TMPDIR", "/tmp")), now=time.time()
        )
        if removed:
            _logger.info("removed %d expired work directories", removed)
        _logger.error("runtime reaper: %s", reap_reason)
        await enqueue_missing(engine, datetime.now(UTC))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await build_worker(
            engine, settings, reproduction_handlers(engine, settings)
        ).run(stop)
    finally:
        await dispose_engine(engine)
    return 0
