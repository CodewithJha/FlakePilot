"""The leased worker against PostgreSQL: handlers, retries, cancellation, shutdown."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.worker import build_worker, job_scope
from flakepilot.orchestration.application.jobs import Job, PermanentJob, RetryableJob
from flakepilot.orchestration.application.worker import Handler, Worker
from flakepilot.orchestration.infrastructure.store import SqlJobStore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
ALLOWED = frozenset({"ping", "ghost"})


class Manual:
    def __init__(self) -> None:
        self.instant = NOW

    def now(self) -> datetime:
        return self.instant


class Step:
    """The claim reads `NOW`; the renewal immediately after reads five seconds later."""

    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        if self.calls == 1:
            return NOW
        return NOW + timedelta(seconds=5)


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _worker(
    engine: AsyncEngine,
    url: str,
    handlers: dict[str, Handler],
    clock: Manual | Step,
    *,
    allowed: frozenset[str] | None = None,
) -> Worker:
    if allowed is None:
        return build_worker(
            engine, Settings(Secret(url)), handlers, clock=clock, owner="worker-a"
        )
    return Worker(
        jobs=job_scope(engine, allowed),
        handlers=handlers,
        clock=clock,
        owner="worker-a",
        lease=timedelta(seconds=30),
        batch=10,
        poll_seconds=30,
    )


async def _enqueue(
    engine: AsyncEngine, job_type: str = "ping", *, allowed: frozenset[str] = ALLOWED
) -> str:
    async with transaction(engine) as session:
        return await SqlJobStore(session, allowed).enqueue(
            job_type=job_type,
            payload={"org_id": "abc-1", "handler": "other"},
            now=NOW,
        )


async def _state(engine: AsyncEngine, job_id: str) -> tuple[str, str | None]:
    async with transaction(engine) as session:
        job = await SqlJobStore(session, ALLOWED).get(job_id)
    assert job is not None
    return job.state, job.last_error


def test_handler_runs_once_and_renews_its_lease(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, datetime | None, list[str]]:
        job_id = await _enqueue(engine)
        seen: list[str] = []

        async def ping(job: Job) -> None:
            assert job.job_type == "ping"
            assert job.payload["handler"] == "other"
            async with transaction(engine) as session:
                current = await SqlJobStore(session, ALLOWED).get(job.id)
            assert current is not None
            assert current.lease_expires_at == NOW + timedelta(seconds=35)
            seen.append(job.id)

        worker = _worker(engine, migrated_database_url, {"ping": ping}, Step())
        await worker.tick_work()
        await worker.tick_work()
        state, _error = await _state(engine, job_id)
        async with transaction(engine) as session:
            done = await SqlJobStore(session, ALLOWED).get(job_id)
        assert done is not None
        return state, done.lease_expires_at, seen

    state, lease, seen = run(migrated_database_url, scenario)
    assert state == "completed" and lease is None and len(seen) == 1


def test_retry_then_success_and_permanent_failure(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[int, str, str]:
        retry_id = await _enqueue(engine)
        stop_id = await _enqueue(engine)
        calls = {"n": 0}
        retried: set[str] = set()

        async def ping(job: Job) -> None:
            calls["n"] += 1
            if job.id == stop_id:
                raise PermanentJob("not_found")
            if job.id not in retried:
                retried.add(job.id)
                raise RetryableJob("rate_limited")

        clock = Manual()
        worker = _worker(engine, migrated_database_url, {"ping": ping}, clock)
        await worker.tick_work()
        await worker.tick_work()
        clock.instant = NOW + timedelta(seconds=1)
        await worker.tick_work()
        retry_state, retry_error = await _state(engine, retry_id)
        stop_state, stop_error = await _state(engine, stop_id)
        assert retry_state == "completed" and retry_error is None
        assert stop_state == "failed" and stop_error == "not_found"
        return calls["n"], stop_state, stop_error or ""

    calls, state, error = run(migrated_database_url, scenario)
    assert calls == 3 and state == "failed" and error == "not_found"


def test_missing_handler_and_secret_text_stay_out_of_the_log(
    migrated_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str | None, str | None]:
        ghost = await _enqueue(engine, "ghost")
        noisy = await _enqueue(engine)

        async def ping(job: Job) -> None:
            raise RuntimeError("super secret value")

        worker = _worker(
            engine,
            migrated_database_url,
            {"ping": ping},
            Manual(),
            allowed=ALLOWED,
        )
        with caplog.at_level(logging.ERROR, logger="flakepilot.worker"):
            await worker.tick_work()
        return (await _state(engine, ghost))[1], (await _state(engine, noisy))[1]

    ghost_error, noisy_error = run(migrated_database_url, scenario)
    assert ghost_error == "unknown_handler"
    assert noisy_error == "handler_error"
    assert "super secret value" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_cancel_during_the_handler_and_shutdown_finishes_the_batch(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, int]:
        cancelled = await _enqueue(engine)
        other = await _enqueue(engine)
        stop = asyncio.Event()
        seen: list[str] = []

        async def ping(job: Job) -> None:
            seen.append(job.id)
            stop.set()
            if job.id == cancelled:
                async with transaction(engine) as session:
                    await SqlJobStore(session, ALLOWED).request_cancel(
                        job_id=job.id, now=NOW
                    )

        worker = _worker(engine, migrated_database_url, {"ping": ping}, Manual())
        await asyncio.wait_for(worker.run(stop), timeout=2)
        cancel_state, _error = await _state(engine, cancelled)
        other_state, _error = await _state(engine, other)
        assert cancel_state == "cancelled"
        assert other_state == "completed"
        return cancel_state, len(seen)

    state, count = run(migrated_database_url, scenario)
    assert state == "cancelled" and count == 2
