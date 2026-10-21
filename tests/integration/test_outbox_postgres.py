"""Outbox delivery to the audit log. No sleeps: locks and events synchronize the tests."""

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.audit.infrastructure.store import SqlAuditStore
from flakepilot.diagnosis.infrastructure.store import SqlDiagStore
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.identity.infrastructure.tables import outbox as iam_outbox
from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.relay import (
    claim_one,
    deliver_batch,
    publish_one,
    to_record,
)
from flakepilot.interface.composition_root.worker import build_worker
from flakepilot.shared_kernel.events import (
    Actor,
    ActorType,
    Channel,
    Event,
    Origin,
    Subject,
)
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh(database_url: str) -> str:
    """One migrated database per test, so delivery counts are not shared."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "FLAKEPILOT_DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return database_url


ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NOW = datetime(2026, 10, 21, tzinfo=UTC)


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _event(name: str, org: str, correlation: str = "corr-1") -> Event:
    return Event(
        name=name,
        actor=Actor(ActorType.SYSTEM),
        origin=Origin(Channel.CLI, correlation),
        subject=Subject("user", org),
        org_id=org,
        payload={"token_id": "abc-1"},
    )


async def _audit_count(engine: AsyncEngine, correlation: str) -> int:
    async with transaction(engine) as session:
        found = await session.execute(
            text("SELECT count(*) FROM aud.events WHERE correlation_id = :c"),
            {"c": correlation},
        )
        return int(found.scalar_one())


async def _pending(engine: AsyncEngine, correlation: str) -> int:
    async with transaction(engine) as session:
        found = await session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM iam.outbox WHERE correlation_id = :c "
                "AND published_at IS NULL AND dead_at IS NULL) + "
                "(SELECT count(*) FROM ti.outbox WHERE correlation_id = :c "
                "AND published_at IS NULL AND dead_at IS NULL) + "
                "(SELECT count(*) FROM diag.outbox WHERE correlation_id = :c "
                "AND published_at IS NULL AND dead_at IS NULL)"
            ),
            {"c": correlation},
        )
        return int(found.scalar_one())


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    async with transaction(engine) as session:
        audit = await session.execute(text("SELECT count(*) FROM aud.events"))
        pending = await session.execute(
            text(
                "SELECT count(*) FROM iam.outbox WHERE published_at IS NULL "
                "AND dead_at IS NULL"
            )
        )
        return int(audit.scalar_one()), int(pending.scalar_one())


def test_three_outboxes_deliver_once_and_keep_the_org(fresh: str) -> None:
    async def scenario(engine: AsyncEngine) -> list[tuple[str, str, str]]:
        async with transaction(engine) as session:
            await SqlIamStore(session).append_event(_event("iam.user.created", ORG))
            await SqlTiStore(session).append_event(_event("ti.upload.accepted", OTHER))
            await SqlDiagStore(session).append_event(
                _event("diag.diagnosis.recorded", ORG)
            )
        assert await deliver_batch(engine, limit=10) == 3
        assert await deliver_batch(engine, limit=10) == 0
        async with transaction(engine) as session:
            rows = await session.execute(
                text(
                    "SELECT source, org_id::text, payload->>'token_id' FROM aud.events"
                )
            )
            return [(str(s), str(o), str(t)) for s, o, t in rows]

    stored = run(fresh, scenario)
    assert sorted(stored) == [
        ("diag", ORG, "abc-1"),
        ("iam", ORG, "abc-1"),
        ("ti", OTHER, "abc-1"),
    ]


def test_duplicate_append_and_redelivery_do_not_duplicate(fresh: str) -> None:
    async def scenario(engine: AsyncEngine) -> int:
        correlation = str(uuid.uuid4())
        async with transaction(engine) as session:
            await SqlIamStore(session).append_event(
                _event("iam.user.created", ORG, correlation)
            )
            row = await claim_one(session, iam_outbox)
            assert row is not None
            record = to_record("iam", row)
            assert record is not None
            store = SqlAuditStore(session)
            assert await store.append(record) is True
            assert await store.append(record) is False
        assert await deliver_batch(engine, limit=10) >= 1
        assert await _pending(engine, correlation) == 0
        return await _audit_count(engine, correlation)

    assert run(fresh, scenario) == 1


def test_poison_stays_visible_and_out_of_the_log(
    fresh: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str | None, bool]:
        event_id = str(uuid.uuid4())
        correlation = str(uuid.uuid4())
        secret = "import os " + ("x" * 120)
        async with transaction(engine) as session:
            await session.execute(
                text(
                    "INSERT INTO iam.outbox (event_id, name, schema_version, occurred_at, "
                    "actor_type, channel, correlation_id, subject_type, subject_id, payload) "
                    "VALUES (:id, 'iam.user.created', 1, now(), 'system', 'cli', :corr, "
                    "'user', :org, CAST(:payload AS jsonb))"
                ),
                {
                    "id": event_id,
                    "org": ORG,
                    "corr": correlation,
                    "payload": '{"note": "' + secret + '"}',
                },
            )
        with caplog.at_level(logging.ERROR, logger="flakepilot.worker"):
            assert await deliver_batch(engine, limit=10) == 1
        async with transaction(engine) as session:
            row = await session.execute(
                text(
                    "SELECT last_error, dead_at IS NOT NULL, published_at IS NULL "
                    "FROM iam.outbox WHERE event_id = :id"
                ),
                {"id": event_id},
            )
            error, dead, unpublished = row.one()
            audit = await session.execute(
                text("SELECT count(*) FROM aud.events WHERE event_id = :id"),
                {"id": event_id},
            )
            assert int(audit.scalar_one()) == 0
            return str(error), bool(dead) and bool(unpublished)

    error, parked = run(fresh, scenario)
    assert error == "invalid_envelope" and parked
    assert "import os" not in caplog.text


def test_uncommitted_event_is_invisible_until_the_commit(fresh: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[int, int]:
        started = asyncio.Event()
        release = asyncio.Event()

        correlation = str(uuid.uuid4())

        async def writer() -> None:
            async with transaction(engine) as session:
                await SqlIamStore(session).append_event(
                    _event("iam.user.created", ORG, correlation)
                )
                started.set()
                await asyncio.wait_for(release.wait(), timeout=2)

        async def reader() -> int:
            await asyncio.wait_for(started.wait(), timeout=2)
            found = await deliver_batch(engine, limit=10)
            release.set()
            return found

        early, _done = await asyncio.gather(reader(), writer())
        await deliver_batch(engine, limit=10)
        return early, await _audit_count(engine, correlation)

    early, later = run(fresh, scenario)
    assert early == 0 and later == 1


def test_concurrent_workers_publish_different_events(fresh: str) -> None:
    async def scenario(engine: AsyncEngine) -> set[str]:
        async with transaction(engine) as session:
            await SqlIamStore(session).append_event(_event("iam.user.created", ORG))
            await SqlIamStore(session).append_event(_event("iam.user.created", OTHER))
        from flakepilot.identity.infrastructure.tables import outbox

        async def take() -> str:
            async with transaction(engine) as session:
                row = await claim_one(session, outbox)
                assert row is not None
                await publish_one(session, "iam", outbox, row)
                return str(row["event_id"])

        left, right = await asyncio.gather(take(), take())
        assert left != right
        audit, pending = await _counts(engine)
        assert audit == 2 and pending == 0
        return {left, right}

    assert len(run(fresh, scenario)) == 2


def test_origin_and_outbox_roll_back_together(fresh: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[int, int]:
        with pytest.raises(RuntimeError):
            async with transaction(engine) as session:
                await SqlIamStore(session).append_event(_event("iam.user.created", ORG))
                raise RuntimeError("boom")
        return await _counts(engine)

    assert run(fresh, scenario) == (0, 0)


def test_worker_tick_delivers_without_a_notification(fresh: str) -> None:
    async def scenario(engine: AsyncEngine) -> int:
        async with transaction(engine) as session:
            await SqlTiStore(session).append_event(_event("ti.upload.accepted", ORG))
        worker = build_worker(
            engine,
            Settings(Secret(fresh)),
            {},
            clock=_Clock(),
            owner="worker-a",
        )
        await worker.tick_work()
        audit, pending = await _counts(engine)
        assert pending == 0
        found = await session_count_ti(engine)
        return audit + found

    assert run(fresh, scenario) == 2


class _Clock:
    def now(self) -> datetime:
        return NOW


async def session_count_ti(engine: AsyncEngine) -> int:
    async with transaction(engine) as session:
        rows = await session.execute(
            text("SELECT count(*) FROM ti.outbox WHERE published_at IS NOT NULL")
        )
        return int(rows.scalar_one())


def test_claim_index_covers_unpublished_live_rows(fresh: str) -> None:
    async def scenario(engine: AsyncEngine) -> str:
        async with transaction(engine) as session:
            rows = await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = "
                    "'ix_iam_outbox_unpublished'"
                )
            )
            return str(rows.scalar_one())

    definition = run(fresh, scenario)
    assert "published_at IS NULL" in definition and "dead_at IS NULL" in definition
