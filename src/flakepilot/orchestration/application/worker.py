"""One leased worker. Handlers are registered in code; a payload cannot name one.

The loop is single-threaded and bounded by the batch size. A database error leaves
the tick, is logged by exception class, and is retried on the next poll. Shutdown
finishes the batch already claimed, then stops. Cancellation is checked after the
handler returns, not by killing it. The job lease is renewed before the handler
and again while it runs. A renewal the store rejects is not completed.
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
        renew_after: float | None = None,
    ) -> None:
        self._jobs = jobs
        self._handlers = dict(handlers)
        self._clock = clock
        self._owner = owner
        self._lease = lease
        self._batch = batch
        self._poll = poll_seconds
        self._deliver = deliver
        self._renew_after = renew_after

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
        stop = asyncio.Event()
        lost = asyncio.Event()
        renewal = asyncio.create_task(self._keep_lease(job, stop, lost))
        try:
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
                if lost.is_set():
                    _logger.error("job lease lost: %s", job.id)
                    return
                _logger.error("job retry: %s %s", job.id, error_code(exc.reason))
                await self._fail(job, exc.reason, permanent=False)
                return
            except PermanentJob as exc:
                if lost.is_set():
                    _logger.error("job lease lost: %s", job.id)
                    return
                _logger.error("job failed: %s %s", job.id, error_code(exc.reason))
                await self._fail(job, exc.reason, permanent=True)
                return
            except Exception as exc:  # noqa: BLE001 - record the failure; do not log the message
                if lost.is_set():
                    _logger.error("job lease lost: %s", job.id)
                    return
                _logger.error("job handler failed: %s %s", job.id, type(exc).__name__)
                await self._fail(job, "handler_error", permanent=False)
                return
            if lost.is_set():
                _logger.error("job lease lost: %s", job.id)
                return
            await self._succeed(job)
        finally:
            stop.set()
            await renewal

    async def _keep_lease(
        self, job: Job, stop: asyncio.Event, lost: asyncio.Event
    ) -> None:
        """Renew until the handler returns. A rejected renewal is not a completion."""
        interval = self._renew_after
        if interval is None:
            interval = max(self._lease.total_seconds() / 3, 1.0)
        elif interval < 0:
            interval = 0.0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                try:
                    async with self._jobs() as store:
                        held = await store.renew(
                            job_id=job.id,
                            owner=self._owner,
                            now=self._clock.now(),
                            lease=self._lease,
                        )
                except Exception as exc:  # noqa: BLE001 - lose the lease; do not log the message
                    _logger.error("job lease renew failed: %s", type(exc).__name__)
                    lost.set()
                    return
                if not held:
                    lost.set()
                    return
            else:
                return

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
