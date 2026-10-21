"""Worker loop: shutdown and database errors. No sleeps and no payloads in logs."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from flakepilot.orchestration.application.jobs import JobStore
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
