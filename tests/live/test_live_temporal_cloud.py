"""The Temporal POC against REAL Temporal Cloud — a billed service.

Owner-run only, and NOT YET RUN: no Cloud credentials existed as of
2026-10-25, so this test's status is NOT VERIFIED. Skipped unless all three
Cloud variables are set; CI never sets them, so CI contacts nothing. Every
workflow execution and action on Cloud is billed, so the scope is two runs.
Needs a throwaway PostgreSQL (`FLAKEPILOT_TEST_DATABASE_URL`) like every
integration test; the worker runs in this process, exactly as the local
dev-server test does — Cloud hosts the workflow state, not the worker:

    FLAKEPILOT_LIVE_TEMPORAL_ADDRESS=<ns>.<id>.tmprl.cloud:7233 \\
    FLAKEPILOT_LIVE_TEMPORAL_NAMESPACE=<ns>.<id> \\
    FLAKEPILOT_LIVE_TEMPORAL_API_KEY=... \\
    FLAKEPILOT_TEST_DATABASE_URL=postgresql://... \\
        uv run pytest tests/live -k temporal_cloud

Two runs of the real workflow and activity around the real investigation of
a seeded run (code-host standing from the test verifier): one workflow, one
recorded investigation, a duplicate start rejected; and a cancelled workflow
records nothing and releases its lock. A skip is not verification.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from agent_route_support import ADMIN_GITHUB_ID, ReadVerifier, Seeded, seed
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import WorkflowAlreadyStartedError

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

_ADDRESS = os.environ.get("FLAKEPILOT_LIVE_TEMPORAL_ADDRESS", "")
_NAMESPACE = os.environ.get("FLAKEPILOT_LIVE_TEMPORAL_NAMESPACE", "")
_API_KEY = os.environ.get("FLAKEPILOT_LIVE_TEMPORAL_API_KEY", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_ADDRESS and _NAMESPACE and _API_KEY),
        reason=(
            "FLAKEPILOT_LIVE_TEMPORAL_ADDRESS, _NAMESPACE and _API_KEY are not "
            "all set (Temporal Cloud is a billed service; owner-run only)"
        ),
    ),
]

_RESULT_TIMEOUT = 90


@asynccontextmanager
async def environment(url: str) -> AsyncIterator[tuple[Client, AsyncEngine, Seeded]]:
    engine = create_engine(url)
    try:
        client = await Client.connect(
            _ADDRESS,
            namespace=_NAMESPACE,
            api_key=_API_KEY,
            tls=True,  # Temporal Cloud: the API key authenticates over TLS
        )
        yield client, engine, await seed(engine)
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
                outcome = await asyncio.wait_for(
                    start_investigation(client, request), _RESULT_TIMEOUT
                )
                with pytest.raises(WorkflowAlreadyStartedError):
                    await start_investigation(client, request)
            return outcome, await _recorded(engine, request.key)

    outcome, rows = asyncio.run(scenario())
    assert (outcome.stopping, outcome.replayed) == ("complete", False)
    assert rows == [("complete",)]
    print(f"temporal cloud: one workflow, {outcome.stages_produced} stages produced")


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
