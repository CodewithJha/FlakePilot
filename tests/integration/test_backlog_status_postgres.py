"""`backlog-status` over real PostgreSQL: real outbox rows and job rows.

Counts and ages only — the command must never print event names, correlation
IDs or payloads, so the test also asserts the output's shape (the leak guard).
`run_backlog_status` manages its own event loop, so it is called from the sync
test body, never inside the seeding scenario. `migrated_database_url` is
shared by this module, so the phases run in order on one throwaway database.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.backlog import run_backlog_status
from flakepilot.interface.composition_root.database import create_engine, transaction

pytestmark = pytest.mark.integration


async def sql(engine: AsyncEngine, statement: str, **params: object) -> list[Any]:
    async with transaction(engine) as session:
        result: Any = await session.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


def test_backlog_status_phases_on_a_migrated_database(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Phase 1: a migrated database with no rows answers all zeros, exit 0.
    code = run_backlog_status(Settings(Secret(migrated_database_url)), [])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    for schema in ("iam", "ti", "diag", "exec", "repo"):
        assert payload["outboxes"][schema] == {
            "pending": 0,
            "dead": 0,
            "oldest_pending_age_seconds": 0,
        }
    assert payload["jobs"] == {}

    # Phase 2: seed one undelivered ti event (with a secret-looking name and a
    # correlation ID), one dead row, a scheduled job and a failed job; the
    # counts match and the content never appears.
    org = str(uuid.uuid4())

    async def seed(engine: AsyncEngine) -> None:
        await sql(
            engine,
            "INSERT INTO ti.outbox (event_id, name, schema_version, occurred_at, "
            "org_id, actor_type, channel, correlation_id, subject_type, subject_id, "
            "payload) VALUES (:i, 'ti.secret.event', 1, now() - interval "
            "'3 seconds', :o, 'system', 'http', 'correlation-leaks', 'upload', "
            ':sid, \'{"k": "v"}\'::jsonb)',
            o=org,
            i=str(uuid.uuid4()),
            sid=str(uuid.uuid4()),
        )
        await sql(
            engine,
            "INSERT INTO ti.outbox (event_id, name, schema_version, occurred_at, "
            "org_id, actor_type, channel, correlation_id, subject_type, subject_id, "
            "payload, dead_at) VALUES (:i, 'ti.dead.event', 1, now() - interval "
            "'1 hour', :o, 'system', 'http', 'correlation-2', 'upload', :sid, "
            "'{}'::jsonb, now())",
            o=org,
            i=str(uuid.uuid4()),
            sid=str(uuid.uuid4()),
        )
        await sql(
            engine,
            "INSERT INTO orch.jobs (id, job_type, payload_version, payload, state, "
            "run_at, attempts, max_attempts, created_at, updated_at, "
            "cancel_requested) VALUES (:j, 'noop', 1, '{}'::jsonb, 'scheduled', "
            "now() - interval '5 seconds', 0, 3, now(), now(), FALSE)",
            j=str(uuid.uuid4()),
        )
        await sql(
            engine,
            "INSERT INTO orch.jobs (id, job_type, payload_version, payload, state, "
            "run_at, attempts, max_attempts, created_at, updated_at, last_error, "
            "cancel_requested) VALUES (:j, 'noop', 1, '{}'::jsonb, 'failed', "
            "now(), 3, 3, now(), now(), 'boom', FALSE)",
            j=str(uuid.uuid4()),
        )

    async def main() -> None:
        engine = create_engine(migrated_database_url)
        try:
            await seed(engine)
        finally:
            await engine.dispose()

    asyncio.run(main())

    code = run_backlog_status(Settings(Secret(migrated_database_url)), [])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    ti = payload["outboxes"]["ti"]
    assert ti["pending"] == 1 and ti["dead"] == 1
    assert 0 <= ti["oldest_pending_age_seconds"] < 60
    assert payload["jobs"]["scheduled"]["count"] == 1
    assert payload["jobs"]["failed"]["count"] == 1
    assert payload["jobs"]["scheduled"]["oldest_age_seconds"] >= 0
    # The leak guard: counts and ages only, never names, IDs or payloads.
    rendered = json.dumps(payload)
    assert "ti.secret.event" not in rendered
    assert "correlation" not in rendered
    assert "'upload'" not in rendered

    # Phase 3: an unreachable database prints the cause class only — never the
    # URL or a driver detail — and exits 1.
    code = run_backlog_status(
        Settings(Secret("postgresql://nobody@localhost:1/none")), []
    )
    assert code == 1
    err = capsys.readouterr().err
    assert err.strip() and "postgresql://" not in err
