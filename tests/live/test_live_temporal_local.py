"""The Temporal POC against a real Temporal server: the local dev server, NOT Temporal Cloud.

Owner-run only. Skipped unless `FLAKEPILOT_LIVE_TEMPORAL_LOCAL=1`, because
`WorkflowEnvironment.start_local()` downloads the official Temporal CLI dev
server binary at test time; CI never sets it. Needs a throwaway PostgreSQL
(`FLAKEPILOT_TEST_DATABASE_URL`) like every integration test:

    FLAKEPILOT_LIVE_TEMPORAL_LOCAL=1 FLAKEPILOT_TEST_DATABASE_URL=postgresql://... \\
        uv run pytest tests/live -k temporal_local

Three runs of the real workflow and activity around the real investigation of
a seeded run (code-host standing from the test verifier): one workflow, one
recorded investigation, a duplicate start rejected; a cancelled workflow
records nothing and releases its lock; and a worker process SIGKILLed
mid-activity, then replaced, still yields exactly one recorded investigation.
"""

import asyncio
import os
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from agent_route_support import ADMIN_GITHUB_ID, ReadVerifier, Seeded, seed
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.database import create_engine
from flakepilot.interface.composition_root.temporal_investigation import (
    InvestigationActivity,
    build_worker,
    failure_reason,
    member_investigation,
    start_investigation,
)
from flakepilot.interface.composition_root.temporal_workflow import (
    ID_REUSE,
    TASK_QUEUE,
    WORKFLOW,
    InvestigationOutcome,
    InvestigationRequest,
    workflow_id,
)
from flakepilot.shared_kernel.events import Origin

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FLAKEPILOT_LIVE_TEMPORAL_LOCAL") != "1",
        reason="FLAKEPILOT_LIVE_TEMPORAL_LOCAL=1 is not set (downloads the dev server)",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
CLI_CACHE = Path(tempfile.gettempdir()) / "flakepilot-temporal-cli"


@asynccontextmanager
async def environment(url: str) -> AsyncIterator[tuple[Client, AsyncEngine, Seeded]]:
    CLI_CACHE.mkdir(exist_ok=True)
    engine = create_engine(url)
    async with await WorkflowEnvironment.start_local(
        download_dest_dir=str(CLI_CACHE)
    ) as env:
        try:
            yield env.client, engine, await seed(engine)
        finally:
            await engine.dispose()


async def _recorded(engine: AsyncEngine, key: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    text(
                        "SELECT after FROM diag.outbox WHERE "
                        "name = 'diag.investigation.recorded' AND correlation_id = :k"
                    ),
                    {"k": key},
                )
            ).all()
        )


async def _advisory_locks(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
        )
    return int(count or 0)


def _request(seeded: Seeded, heartbeat: int = 30) -> InvestigationRequest:
    return InvestigationRequest(
        str(uuid.uuid4()), seeded.org_id, seeded.run_id, ADMIN_GITHUB_ID, heartbeat
    )


def test_one_workflow_records_one_investigation_and_a_duplicate_is_rejected(
    migrated_database_url: str,
) -> None:
    async def scenario() -> Any:
        async with environment(migrated_database_url) as (client, engine, seeded):
            real = member_investigation(
                engine, Settings(Secret(migrated_database_url)), ReadVerifier()
            )
            activity = InvestigationActivity(engine, real, heartbeat_every=0.5)
            request = _request(seeded)
            async with build_worker(client, activity):
                outcome = await start_investigation(client, request)
                with pytest.raises(WorkflowAlreadyStartedError):
                    await start_investigation(client, request)
            return outcome, await _recorded(engine, request.key)

    outcome, rows = asyncio.run(scenario())
    assert (outcome.stopping, outcome.replayed) == ("complete", False)
    assert rows == [("complete",)]
    print(f"temporal local: one workflow, {outcome.stages_produced} stages produced")


def test_cancelling_the_workflow_stops_the_activity_and_records_nothing(
    migrated_database_url: str,
) -> None:
    async def scenario() -> Any:
        async with environment(migrated_database_url) as (client, engine, seeded):
            started = asyncio.Event()

            async def hang(request: InvestigationRequest, origin: Origin) -> Any:
                started.set()
                await asyncio.sleep(3600)

            activity = InvestigationActivity(engine, hang, heartbeat_every=0.2)
            request = _request(seeded, heartbeat=5)
            async with build_worker(client, activity):
                handle = await client.start_workflow(
                    WORKFLOW,
                    request,
                    id=workflow_id(request.key),
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=ID_REUSE,
                    result_type=InvestigationOutcome,
                )
                await asyncio.wait_for(started.wait(), 30)
                await handle.cancel()
                with pytest.raises(WorkflowFailureError) as failed:
                    await asyncio.wait_for(handle.result(), 30)
            return (
                failure_reason(failed.value),
                await _recorded(engine, request.key),
                await _advisory_locks(engine),
            )

    reason, rows, locks = asyncio.run(scenario())
    assert reason == "cancelled"
    assert rows == [] and locks == 0


def test_a_worker_killed_mid_activity_still_yields_exactly_one_investigation(
    migrated_database_url: str, tmp_path: Path
) -> None:
    marker = tmp_path / "activity-started"

    async def scenario() -> Any:
        async with environment(migrated_database_url) as (client, engine, seeded):
            address = client.service_client.config.target_host
            doomed = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "tests/temporal_kill_worker.py"),
                address,
                migrated_database_url,
                str(marker),
                cwd=ROOT,
            )
            try:
                request = _request(seeded, heartbeat=3)
                handle = await client.start_workflow(
                    WORKFLOW,
                    request,
                    id=workflow_id(request.key),
                    task_queue=TASK_QUEUE,
                    id_reuse_policy=ID_REUSE,
                    result_type=InvestigationOutcome,
                )
                deadline = time.monotonic() + 60
                while not marker.exists():
                    assert time.monotonic() < deadline, (
                        "the doomed worker never started"
                    )
                    assert doomed.returncode is None, "the doomed worker exited early"
                    await asyncio.sleep(0.2)
                locks_while_running = await _advisory_locks(engine)
                doomed.send_signal(signal.SIGKILL)
                await asyncio.wait_for(doomed.wait(), 10)
            finally:
                if doomed.returncode is None:
                    doomed.kill()
            real = member_investigation(
                engine, Settings(Secret(migrated_database_url)), ReadVerifier()
            )
            activity = InvestigationActivity(engine, real, heartbeat_every=0.5)
            async with build_worker(client, activity):
                outcome = await asyncio.wait_for(handle.result(), 90)
            attempts = [
                event.activity_task_started_event_attributes.attempt
                async for event in handle.fetch_history_events()
                if event.HasField("activity_task_started_event_attributes")
            ]
            return (
                outcome,
                locks_while_running,
                attempts,
                await _recorded(engine, request.key),
                await _advisory_locks(engine),
            )

    outcome, locks_while_running, attempts, rows, locks_after = asyncio.run(scenario())
    assert locks_while_running == 1  # the killed worker held the key's lock
    assert (outcome.stopping, outcome.replayed) == ("complete", False)
    assert attempts[-1] >= 2  # the replacement worker ran a retry attempt
    assert rows == [("complete",)]  # exactly one recorded investigation
    assert locks_after == 0
    print(
        f"temporal local: worker SIGKILLed mid-activity; attempt {attempts[-1]} "
        "on the replacement recorded exactly one investigation"
    )
