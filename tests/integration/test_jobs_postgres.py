"""Job claim, lease, and recovery against PostgreSQL. Time moves by passing a clock."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.orchestration.domain.jobs import InvalidJob
from flakepilot.orchestration.infrastructure.store import SqlJobStore

pytestmark = pytest.mark.integration

ALLOWED = frozenset({"ping"})
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
LEASE = timedelta(seconds=30)


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def _enqueue(engine: AsyncEngine, *, max_attempts: int = 5) -> str:
    async with transaction(engine) as session:
        return await SqlJobStore(session, ALLOWED).enqueue(
            job_type="ping",
            payload={"org_id": "abc-1"},
            now=NOW,
            max_attempts=max_attempts,
            correlation_id="corr-1",
        )


def test_invalid_jobs_write_nothing(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> int:
        async with transaction(engine) as session:
            store = SqlJobStore(session, ALLOWED)
            with pytest.raises(InvalidJob):
                await store.enqueue(
                    job_type="ping",
                    payload={"note": "import os"},
                    now=NOW,
                )
            with pytest.raises(InvalidJob):
                await store.enqueue(job_type="exec", payload={}, now=NOW)
            found = await session.execute(text("SELECT count(*) FROM orch.jobs"))
            return int(found.scalar_one())

    assert run(migrated_database_url, scenario) == 0


def test_enqueue_rolls_back_with_the_caller(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> int:
        with pytest.raises(RuntimeError):
            async with transaction(engine) as session:
                await SqlJobStore(session, ALLOWED).enqueue(
                    job_type="ping", payload={"org_id": "abc-1"}, now=NOW
                )
                raise RuntimeError("boom")
        async with transaction(engine) as session:
            found = await session.execute(text("SELECT count(*) FROM orch.jobs"))
            return int(found.scalar_one())

    assert run(migrated_database_url, scenario) == 0


def test_stale_owner_cannot_ack_or_renew(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[bool, bool, str]:
        job_id = await _enqueue(engine)
        async with transaction(engine) as session:
            store = SqlJobStore(session, ALLOWED)
            claimed = await store.claim(owner="worker-a", now=NOW, limit=1, lease=LEASE)
            assert [job.id for job in claimed] == [job_id]
            assert claimed[0].attempts == 1
            assert (
                await store.complete(job_id=job_id, owner="worker-b", now=NOW) is False
            )
            renewed = await store.renew(
                job_id=job_id, owner="worker-b", now=NOW, lease=LEASE
            )
            assert renewed is False
            assert (
                await store.complete(job_id=job_id, owner="worker-a", now=NOW) is True
            )
        async with transaction(engine) as session:
            job = await SqlJobStore(session, ALLOWED).get(job_id)
            assert job is not None
            return renewed, job.state == "completed", job.correlation_id or ""

    renewed, completed, correlation = run(migrated_database_url, scenario)
    assert renewed is False and completed and correlation == "corr-1"


def test_expired_lease_is_reclaimed_and_then_exhausted(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[int, str, str | None]:
        job_id = await _enqueue(engine, max_attempts=1)
        later = NOW + LEASE
        async with transaction(engine) as session:
            store = SqlJobStore(session, ALLOWED)
            first = await store.claim(owner="worker-a", now=NOW, limit=1, lease=LEASE)
            assert first[0].lease_expires_at == later
            assert (
                await store.complete(job_id=job_id, owner="worker-a", now=later)
                is False
            )
            assert (
                await store.renew(
                    job_id=job_id, owner="worker-a", now=later, lease=LEASE
                )
                is False
            )
        async with transaction(engine) as session:
            second = await SqlJobStore(session, ALLOWED).claim(
                owner="worker-b", now=later, limit=1, lease=LEASE
            )
            assert second == []
            job = await SqlJobStore(session, ALLOWED).get(job_id)
            assert job is not None
            return job.attempts, job.state, job.last_error

    attempts, state, error = run(migrated_database_url, scenario)
    assert attempts == 1 and state == "failed" and error == "exhausted"


def test_retry_waits_for_backoff_then_a_permanent_failure_stops(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, str]:
        job_id = await _enqueue(engine)
        async with transaction(engine) as session:
            store = SqlJobStore(session, ALLOWED)
            await store.claim(owner="worker-a", now=NOW, limit=1, lease=LEASE)
            assert await store.fail(
                job_id=job_id,
                owner="worker-a",
                now=NOW,
                error="connection to db.internal failed",
                permanent=False,
            )
        async with transaction(engine) as session:
            store = SqlJobStore(session, ALLOWED)
            early = await store.claim(owner="worker-b", now=NOW, limit=1, lease=LEASE)
            assert early == []
            due = await store.claim(
                owner="worker-b",
                now=NOW + timedelta(seconds=1),
                limit=1,
                lease=LEASE,
            )
            assert due[0].id == job_id and due[0].attempts == 2
            assert due[0].last_error == "invalid_error"
            assert await store.fail(
                job_id=job_id,
                owner="worker-b",
                now=NOW + timedelta(seconds=1),
                error="not_found",
                permanent=True,
            )
            job = await store.get(job_id)
            assert job is not None
            return job.state, job.last_error or ""

    state, error = run(migrated_database_url, scenario)
    assert state == "failed" and error == "not_found"


def test_cancel_is_a_flag_checked_at_claim(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> str:
        job_id = await _enqueue(engine)
        async with transaction(engine) as session:
            store = SqlJobStore(session, ALLOWED)
            assert await store.request_cancel(job_id=job_id, now=NOW) is True
            assert (
                await store.claim(owner="worker-a", now=NOW, limit=1, lease=LEASE) == []
            )
            job = await store.get(job_id)
            assert job is not None and job.state == "cancelled"
            held = await _enqueue(engine)
            await store.claim(owner="worker-a", now=NOW, limit=1, lease=LEASE)
            assert await store.request_cancel(job_id=held, now=NOW) is True
            assert await store.complete(job_id=held, owner="worker-a", now=NOW) is False
            assert await store.acknowledge_cancel(
                job_id=held, owner="worker-a", now=NOW
            )
            finished = await store.get(held)
            assert finished is not None
            return finished.state

    assert run(migrated_database_url, scenario) == "cancelled"


def test_invalid_stored_payload_fails_closed(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> str | None:
        job_id = str(uuid.uuid4())
        async with transaction(engine) as session:
            await session.execute(
                text(
                    "INSERT INTO orch.jobs (id, job_type, payload_version, payload, "
                    "state, run_at, attempts, max_attempts, created_at, updated_at, "
                    "cancel_requested) VALUES (:id, 'ping', 1, CAST(:payload AS jsonb), "
                    "'scheduled', :now, 0, 5, :now, :now, false)"
                ),
                {"id": job_id, "payload": '{"note": "import os"}', "now": NOW},
            )
            claimed = await SqlJobStore(session, ALLOWED).claim(
                owner="worker-a", now=NOW, limit=1, lease=LEASE
            )
            assert claimed == []
            job = await SqlJobStore(session, ALLOWED).get(job_id)
            assert job is not None
            return job.last_error

    assert run(migrated_database_url, scenario) == "invalid_payload"


def test_concurrent_workers_claim_different_jobs(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[set[str], set[str]]:
        first = await _enqueue(engine)
        second = await _enqueue(engine)

        async def take(owner: str) -> set[str]:
            async with transaction(engine) as session:
                claimed = await SqlJobStore(session, ALLOWED).claim(
                    owner=owner, now=NOW, limit=1, lease=LEASE
                )
                return {job.id for job in claimed}

        left, right = await asyncio.gather(take("worker-a"), take("worker-b"))
        assert left.isdisjoint(right)
        assert left | right == {first, second}
        async with transaction(engine) as session:
            again = await SqlJobStore(session, ALLOWED).claim(
                owner="worker-c", now=NOW, limit=5, lease=LEASE
            )
            return left, {job.id for job in again}

    claimed, leftover = run(migrated_database_url, scenario)
    assert claimed and leftover == set()


def test_claim_indexes_match_the_queries(migrated_database_url: str) -> None:
    async def scenario(engine: AsyncEngine) -> str:
        async with transaction(engine) as session:
            rows = await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname IN "
                    "('ix_orch_jobs_due', 'ix_orch_jobs_expired') ORDER BY indexname"
                )
            )
            return " ".join(rows.scalars())

    definition = run(migrated_database_url, scenario)
    assert "state = 'scheduled'" in definition
    assert "run_at" in definition
    assert "state = 'running'" in definition
    assert "lease_expires_at" in definition
