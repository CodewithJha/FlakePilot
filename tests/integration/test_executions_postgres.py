"""Execution records against PostgreSQL. Clocks move by argument, not by sleeping."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from flakepilot.execution.application.reproduction import schedule
from flakepilot.execution.domain.reproduction import RUNNING, finish
from flakepilot.execution.infrastructure.store import SqlExecutionStore
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    OrgAccess,
    ResourceNotFound,
)
from flakepilot.integrations.application.ports import CodeHostError
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.relay import deliver_batch
from flakepilot.interface.composition_root.scheduling import enqueue_browser_job
from flakepilot.repository.application.source import RepoBinding, new_binding
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Actor, ActorType, Channel, Origin

pytestmark = pytest.mark.integration

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
SHA = "ab" * 20
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
SELECTOR = "tests.login.TestLogin.test_ok"


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


class _Host:
    def __init__(self, sha: str | None) -> None:
        self._sha = sha
        self.calls = 0

    async def confirm_commit(self, *, owner: str, name: str, claimed_sha: str) -> str:
        self.calls += 1
        if self._sha is None:
            raise CodeHostError("source_unverified", unavailable=True)
        return self._sha

    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes:
        return b""


async def _register(session: AsyncSession, owner: str = "acme") -> RepoBinding:
    binding = new_binding(org_id=ORG, owner=owner, name=f"web-{uuid.uuid4().hex[:8]}")
    await SqlRepoDirectory(session).add(binding, now=NOW)
    return binding


def test_unverified_sha_is_inconclusive_and_rolls_back(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, int, int]:
        host = _Host(None)
        async with transaction(engine) as session:
            binding = await _register(session)
            record = await schedule(
                SqlRepoDirectory(session),
                host,
                SqlExecutionStore(session),
                access=OrgAccess(USER, ORG, org_admin=True),
                code_host=CodeHostAccess.READ,
                org_id=ORG,
                repo_id=binding.repo_id,
                selector=SELECTOR,
                claimed_sha=SHA,
                actor=Actor(ActorType.USER, USER),
                origin=Origin(Channel.CLI, "corr-unverified"),
                now=NOW,
            )
        with pytest.raises(RuntimeError):
            async with transaction(engine) as session:
                await schedule(
                    SqlRepoDirectory(session),
                    host,
                    SqlExecutionStore(session),
                    access=OrgAccess(USER, ORG, org_admin=True),
                    code_host=CodeHostAccess.READ,
                    org_id=ORG,
                    repo_id=binding.repo_id,
                    selector=SELECTOR,
                    claimed_sha=SHA,
                    actor=Actor(ActorType.USER, USER),
                    origin=Origin(Channel.CLI, "corr-rollback"),
                    now=NOW,
                )
                raise RuntimeError("boom")
        async with transaction(engine) as session:
            kept = await session.execute(
                text(
                    "SELECT count(*) FROM exec.executions "
                    "WHERE correlation_id = 'corr-rollback'"
                )
            )
            events = await session.execute(
                text(
                    "SELECT count(*) FROM exec.outbox "
                    "WHERE correlation_id = 'corr-rollback'"
                )
            )
            return (
                record.outcome or "",
                int(kept.scalar_one()),
                int(events.scalar_one()),
            )

    outcome, executions, events = run(migrated_database_url, scenario)
    assert outcome == "inconclusive"
    assert (executions, events) == (0, 0)


def test_pin_schedules_once_and_a_stale_lease_cannot_finish(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, str, bool, str]:
        async with transaction(engine) as session:
            binding = await _register(session, owner="beta")
            directory = SqlRepoDirectory(session)
            records = SqlExecutionStore(session)
            first = await schedule(
                directory,
                _Host(SHA),
                records,
                access=OrgAccess(USER, ORG, org_admin=True),
                code_host=CodeHostAccess.READ,
                org_id=ORG,
                repo_id=binding.repo_id,
                selector=SELECTOR,
                claimed_sha=SHA,
                actor=Actor(ActorType.USER, USER),
                origin=Origin(Channel.CLI, "corr-pin"),
                now=NOW,
            )
            second = await schedule(
                directory,
                _Host(SHA),
                records,
                access=OrgAccess(USER, ORG, org_admin=True),
                code_host=CodeHostAccess.READ,
                org_id=ORG,
                repo_id=binding.repo_id,
                selector=SELECTOR,
                claimed_sha=SHA,
                actor=Actor(ActorType.USER, USER),
                origin=Origin(Channel.CLI, "corr-pin"),
                now=NOW,
            )
            assert first.id == second.id
            execution_id = first.id
        job_id = await enqueue_browser_job(engine, execution_id, NOW)
        async with transaction(engine) as session:
            claimed = await SqlExecutionStore(session).claim(
                execution_id=execution_id,
                owner="worker-a",
                now=NOW,
                lease=timedelta(seconds=30),
            )
            assert claimed is not None
            generation = claimed.generation
        async with transaction(engine) as session:
            stale = await SqlExecutionStore(session).finish(
                execution_id=execution_id,
                owner="worker-b",
                generation=generation,
                now=NOW,
                decision=finish(RUNNING, "success", "passed"),
                evidence_partial=False,
                actor=Actor(ActorType.SYSTEM),
                origin=Origin(Channel.CLI, "corr-pin"),
            )
        async with transaction(engine) as session:
            current = await SqlExecutionStore(session).get(execution_id)
            assert current is not None
            return job_id or "", current.state, stale, current.sha

    job_id, state, stale, sha = run(migrated_database_url, scenario)
    assert uuid.UUID(job_id)
    assert state == "running"
    assert stale is False
    assert sha == SHA


def test_missing_grant_writes_nothing_and_delivery_keeps_the_org(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[int, str]:
        async with transaction(engine) as session:
            binding = await _register(session, owner="gamma")
            with pytest.raises(ResourceNotFound):
                await schedule(
                    SqlRepoDirectory(session),
                    _Host(SHA),
                    SqlExecutionStore(session),
                    access=OrgAccess(USER, ORG),
                    code_host=CodeHostAccess.NONE,
                    org_id=ORG,
                    repo_id=binding.repo_id,
                    selector=SELECTOR,
                    claimed_sha=SHA,
                    actor=Actor(ActorType.USER, USER),
                    origin=Origin(Channel.CLI, "corr-denied"),
                    now=NOW,
                )
        async with transaction(engine) as session:
            binding = await _register(session, owner="delta")
            record = await schedule(
                SqlRepoDirectory(session),
                _Host(SHA),
                SqlExecutionStore(session),
                access=OrgAccess(USER, ORG, org_admin=True),
                code_host=CodeHostAccess.READ,
                org_id=ORG,
                repo_id=binding.repo_id,
                selector="tests.other.TestOther.test_ok",
                claimed_sha=SHA,
                actor=Actor(ActorType.USER, USER),
                origin=Origin(Channel.CLI, "corr-deliver"),
                now=NOW,
            )
            execution_id = record.id
        delivered = await deliver_batch(engine, 10)
        async with transaction(engine) as session:
            found = await session.execute(
                text(
                    "SELECT org_id::text FROM aud.events "
                    "WHERE correlation_id = 'corr-deliver'"
                )
            )
            org_id = found.scalar_one()
            denied = await session.execute(
                text(
                    "SELECT count(*) FROM exec.executions "
                    "WHERE correlation_id = 'corr-denied'"
                )
            )
            return int(denied.scalar_one()), f"{delivered}:{org_id}:{execution_id[:8]}"

    denied, marker = run(migrated_database_url, scenario)
    assert denied == 0
    assert ORG in marker
