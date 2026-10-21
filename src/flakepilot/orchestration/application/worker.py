"""One leased worker. Handlers are registered in code; a payload cannot name one.

The loop is single-threaded and bounded by the batch size. A database error leaves
the tick, is logged by exception class, and is retried on the next poll. Shutdown
finishes the batch already claimed, then stops. Cancellation is checked after the
handler returns, not by killing it.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

from flakepilot.orchestration.application.jobs import (
    Clock,
    Job,
    JobStore,
    PermanentJob,
    RetryableJob,
)
from flakepilot.orchestration.domain.jobs import error_code

_logger = logging.getLogger("flakepilot.worker")

Jobs = Callable[[], AbstractAsyncContextManager[JobStore]]
Deliver = Callable[[], Awaitable[None]]
Handler = Callable[[Job], Awaitable[None]]


class Worker:
    def __init__(
        self,
        *,
        jobs: Jobs,
        handlers: Mapping[str, Handler],
        clock: Clock,
        owner: str,
        lease: timedelta,
        batch: int,
        poll_seconds: float,
        deliver: Deliver | None = None,
    ) -> None:
        self._jobs = jobs
        self._handlers = dict(handlers)
        self._clock = clock
        self._owner = owner
        self._lease = lease
        self._batch = batch
        self._poll = poll_seconds
        self._deliver = deliver

    async def tick_work(self) -> None:
        await self._run_jobs()
        if self._deliver is not None:
            await self._deliver()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick_work()
            except Exception as exc:  # noqa: BLE001 - keep polling after a driver or handler fault
                _logger.error("worker tick failed: %s", type(exc).__name__)
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll)
            except TimeoutError:
                continue

    async def _run_jobs(self) -> None:
        async with self._jobs() as store:
            claimed = await store.claim(
                owner=self._owner,
                now=self._clock.now(),
                limit=self._batch,
                lease=self._lease,
            )
        for job in claimed:
            await self._finish(job)

    async def _finish(self, job: Job) -> None:
        async with self._jobs() as store:
            held = await store.renew(
                job_id=job.id,
                owner=self._owner,
                now=self._clock.now(),
                lease=self._lease,
            )
        if not held:
            _logger.error("job lease lost: %s", job.id)
            return
        handler = self._handlers.get(job.job_type)
        if handler is None:
            await self._fail(job, "unknown_handler", permanent=True)
            return
        try:
            await handler(job)
        except RetryableJob as exc:
            _logger.error("job retry: %s %s", job.id, error_code(exc.reason))
            await self._fail(job, exc.reason, permanent=False)
            return
        except PermanentJob as exc:
            _logger.error("job failed: %s %s", job.id, error_code(exc.reason))
            await self._fail(job, exc.reason, permanent=True)
            return
        except Exception as exc:  # noqa: BLE001 - record the failure; do not log the message
            _logger.error("job handler failed: %s %s", job.id, type(exc).__name__)
            await self._fail(job, "handler_error", permanent=False)
            return
        await self._succeed(job)

    async def _succeed(self, job: Job) -> None:
        async with self._jobs() as store:
            fresh = await store.get(job.id)
            now = self._clock.now()
            cancelled = (
                fresh is not None
                and fresh.cancel_requested
                and await store.acknowledge_cancel(
                    job_id=job.id, owner=self._owner, now=now
                )
            )
            if cancelled:
                _logger.info("job cancelled: %s", job.id)
                return
            ok = await store.complete(job_id=job.id, owner=self._owner, now=now)
        if ok:
            _logger.info("job completed: %s", job.id)
        else:
            _logger.error("job completion rejected: %s", job.id)

    async def _fail(self, job: Job, reason: str, *, permanent: bool) -> None:
        async with self._jobs() as store:
            ok = await store.fail(
                job_id=job.id,
                owner=self._owner,
                now=self._clock.now(),
                error=reason,
                permanent=permanent,
            )
        if not ok:
            _logger.error("job failure rejected: %s", job.id)
