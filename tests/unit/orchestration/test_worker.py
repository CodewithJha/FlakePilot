"""Worker loop: shutdown and database errors. No sleeps and no payloads in logs."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from flakepilot.orchestration.application.jobs import Job, JobStore
from flakepilot.orchestration.application.worker import Worker

NOW = datetime(2026, 10, 21, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class _Closed:
    async def __aenter__(self) -> JobStore:
        raise OSError("db.internal.example")

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _jobs() -> _Closed:
    return _Closed()


def _worker(kind: type[Worker]) -> Worker:
    return kind(
        jobs=_jobs,
        handlers={},
        clock=Clock(),
        owner="worker-a",
        lease=timedelta(seconds=30),
        batch=10,
        poll_seconds=30,
    )


def test_shutdown_returns_without_waiting() -> None:
    stop = asyncio.Event()

    class Stop(Worker):
        async def tick_work(self) -> None:
            stop.set()

    asyncio.run(asyncio.wait_for(_worker(Stop).run(stop), timeout=1))


def test_database_outage_is_logged_without_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()

    class Boom(Worker):
        async def tick_work(self) -> None:
            stop.set()
            raise OSError("db.internal.example")

    with caplog.at_level(logging.ERROR, logger="flakepilot.worker"):
        asyncio.run(asyncio.wait_for(_worker(Boom).run(stop), timeout=1))
    assert "OSError" in caplog.text
    assert "db.internal.example" not in caplog.text


def _job() -> Job:
    return Job(
        id="11111111-1111-1111-1111-111111111111",
        job_type="browser_reproduction",
        payload_version=1,
        payload={"execution_id": "22222222-2222-2222-2222-222222222222"},
        state="running",
        run_at=NOW,
        attempts=1,
        max_attempts=5,
        lease_owner="worker-a",
        lease_expires_at=NOW,
        last_error=None,
        correlation_id="corr-job",
        causation_id=None,
        cancel_requested=False,
    )


class _LeaseStore:
    def __init__(self, *, hold: bool) -> None:
        self.renews = 0
        self.completed = 0
        self._hold = hold
        self.allow = True

    async def enqueue(self, **kwargs: object) -> str:
        del kwargs
        raise AssertionError("enqueue")

    async def claim(self, **kwargs: object) -> list[Job]:
        del kwargs
        raise AssertionError("claim")

    async def fail(self, **kwargs: object) -> bool:
        del kwargs
        raise AssertionError("fail")

    async def request_cancel(self, **kwargs: object) -> bool:
        del kwargs
        raise AssertionError("request_cancel")

    async def renew(
        self, *, job_id: str, owner: str, now: datetime, lease: timedelta
    ) -> bool:
        del job_id, owner, now, lease
        self.renews += 1
        if not self._hold:
            self.allow = self.renews < 2
        return self.allow

    async def get(self, job_id: str) -> Job | None:
        del job_id
        return _job()

    async def acknowledge_cancel(
        self, *, job_id: str, owner: str, now: datetime
    ) -> bool:
        del job_id, owner, now
        return False

    async def complete(self, *, job_id: str, owner: str, now: datetime) -> bool:
        del job_id, owner, now
        self.completed += 1
        return True


class _Open:
    def __init__(self, store: _LeaseStore) -> None:
        self._store = store

    async def __aenter__(self) -> JobStore:
        return self._store

    async def __aexit__(self, *_exc: object) -> None:
        return None


def test_a_long_handler_renews_until_it_returns() -> None:
    store = _LeaseStore(hold=True)

    async def handle(job: Job) -> None:
        del job
        while store.renews < 2:
            await asyncio.sleep(0)

    worker = Worker(
        jobs=lambda: _Open(store),
        handlers={"browser_reproduction": handle},
        clock=Clock(),
        owner="worker-a",
        lease=timedelta(seconds=30),
        batch=1,
        poll_seconds=30,
        renew_after=0,
    )
    asyncio.run(asyncio.wait_for(worker._finish(_job()), timeout=1))
    assert store.renews >= 2
    assert store.completed == 1


def test_a_lost_job_lease_is_not_completed() -> None:
    store = _LeaseStore(hold=False)

    async def handle(job: Job) -> None:
        del job
        while store.renews < 2:
            await asyncio.sleep(0)

    worker = Worker(
        jobs=lambda: _Open(store),
        handlers={"browser_reproduction": handle},
        clock=Clock(),
        owner="worker-a",
        lease=timedelta(seconds=30),
        batch=1,
        poll_seconds=30,
        renew_after=0,
    )
    asyncio.run(asyncio.wait_for(worker._finish(_job()), timeout=1))
    assert store.renews >= 2
    assert store.completed == 0
