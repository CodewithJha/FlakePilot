"""The Temporal POC's activity on real PostgreSQL, without a Temporal server (goal 5).

`ActivityEnvironment` runs the activity function in-process, so CI exercises
FlakePilot's authority: the advisory lock, the existing-outbox-row check, and
the reason codes (retryable or final), around the real investigation of a
seeded run. The workflow itself runs against a real local Temporal server in
`tests/live/test_live_temporal_local.py`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from agent_route_support import ADMIN_GITHUB_ID, ReadVerifier, seed
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from flakepilot.identity.application.authorization import PermissionDenied
from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.database import create_engine
from flakepilot.interface.composition_root.temporal_investigation import (
    InvestigationActivity,
    member_investigation,
)
from flakepilot.interface.composition_root.temporal_workflow import (
    InvestigationRequest,
)
from flakepilot.shared_kernel.events import Origin

pytestmark = pytest.mark.integration


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def _recorded(engine: AsyncEngine, key: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    text(
                        "SELECT after, actor_id::text FROM diag.outbox WHERE "
                        "name = 'diag.investigation.recorded' AND correlation_id = :k"
                    ),
                    {"k": key},
                )
            ).all()
        )


def test_one_key_runs_once_and_a_repeat_replays_the_recorded_outcome(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        seeded = await seed(engine)
        real = member_investigation(
            engine, Settings(Secret(migrated_database_url)), verifier=ReadVerifier()
        )
        calls: list[str] = []

        async def investigate(request: InvestigationRequest, origin: Origin) -> Any:
            calls.append(origin.correlation_id)
            return await real(request, origin)

        activity = InvestigationActivity(engine, investigate, heartbeat_every=0.1)
        request = InvestigationRequest(
            str(uuid.uuid4()), seeded.org_id, seeded.run_id, ADMIN_GITHUB_ID
        )
        env = ActivityEnvironment()
        first = await env.run(activity.run, request)
        again = await env.run(activity.run, request)
        return (
            seeded,
            request,
            calls,
            first,
            again,
            await _recorded(engine, request.key),
        )

    seeded, request, calls, first, again, rows = run(migrated_database_url, scenario)
    assert calls == [request.key]  # the repeat did not run the investigation
    assert (first.stopping, first.replayed) == ("complete", False)
    assert first.stages_produced >= 2  # collect and prioritize at least
    assert (again.stopping, again.stages_produced, again.replayed) == (
        "complete",
        first.stages_produced,
        True,
    )
    assert rows == [("complete", seeded.admin_id)]


def test_a_held_lock_is_retryable_and_the_lock_is_released_after_each_run(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        async def never(request: InvestigationRequest, origin: Origin) -> Any:
            raise AssertionError("must not run while the lock is held")

        activity = InvestigationActivity(engine, never)
        key = str(uuid.uuid4())
        request = InvestigationRequest(key, str(uuid.uuid4()), str(uuid.uuid4()), "1")
        async with engine.connect() as holder:
            assert await holder.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
                {"k": f"flakepilot.investigation:{key}"},
            )
            with pytest.raises(ApplicationError) as held:
                await ActivityEnvironment().run(activity.run, request)
            await holder.execute(text("SELECT pg_advisory_unlock_all()"))
        async with engine.connect() as probe:
            locks = await probe.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            )
        return held.value, locks

    held, locks = run(migrated_database_url, scenario)
    assert (held.type, held.non_retryable) == ("investigation_in_progress", False)
    assert locks == 0


@pytest.mark.parametrize(
    ("raised", "reason", "retryable"),
    [
        (None, "not_found", False),  # unknown member: IAM finds nobody
        (PermissionDenied(), "unauthorized", False),
        (RuntimeError("detail that must not leak"), "investigation_failed", False),
        (OSError("db host 10.0.0.1 unreachable"), "database_unavailable", True),
    ],
)
def test_failures_map_to_reason_codes_and_release_the_lock(
    migrated_database_url: str,
    raised: Exception | None,
    reason: str,
    retryable: bool,
) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        real = member_investigation(
            engine, Settings(Secret(migrated_database_url)), verifier=ReadVerifier()
        )

        async def investigate(request: InvestigationRequest, origin: Origin) -> Any:
            if raised is not None:
                raise raised
            return await real(request, origin)

        request = InvestigationRequest(
            str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), "999999"
        )
        with pytest.raises(ApplicationError) as failed:
            await ActivityEnvironment().run(
                InvestigationActivity(engine, investigate).run, request
            )
        async with engine.connect() as probe:
            locks = await probe.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            )
        return failed.value, locks, await _recorded(engine, request.key)

    error, locks, rows = run(migrated_database_url, scenario)
    assert (error.type, error.non_retryable) == (reason, not retryable)
    assert "leak" not in str(error) and "10.0.0.1" not in str(error)
    assert locks == 0 and rows == []


def test_a_malformed_request_is_final_before_any_database_work(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> ApplicationError:
        async def never(request: InvestigationRequest, origin: Origin) -> Any:
            raise AssertionError

        request = InvestigationRequest("not-a-uuid", "x", "y", "abc")
        with pytest.raises(ApplicationError) as refused:
            await ActivityEnvironment().run(
                InvestigationActivity(engine, never).run, request
            )
        return refused.value

    error = run(migrated_database_url, scenario)
    assert (error.type, error.non_retryable) == ("invalid_request", True)
