"""The worker persists a control-plane outcome. Closed mode fetches nothing."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.execution.application.reproduction import schedule
from flakepilot.execution.infrastructure.store import SqlExecutionStore
from flakepilot.identity.application.authorization import CodeHostAccess, OrgAccess
from flakepilot.integrations.application.ports import (
    ArtifactRef,
    ArtifactStoreError,
    CodeHostError,
    SandboxFile,
    SandboxReport,
)
from flakepilot.integrations.infrastructure.artifact_store import LocalFsArtifactStore
from flakepilot.integrations.infrastructure.sandbox import PNG_MAGIC, ClosedSandbox
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.reproduction import reproduce
from flakepilot.interface.composition_root.scheduling import enqueue_missing
from flakepilot.orchestration.application.jobs import RetryableJob
from flakepilot.repository.application.source import new_binding
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Actor, ActorType, Channel, Origin

pytestmark = pytest.mark.integration

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
SHA = "ef" * 20
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
SELECTOR = "tests.login.TestLogin.test_ok"


def _root(path: Path) -> Path:
    path.chmod(0o700)
    return path


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


class _Host:
    def __init__(self, sha: str) -> None:
        self._sha = sha
        self.fetches = 0

    async def confirm_commit(self, *, owner: str, name: str, claimed_sha: str) -> str:
        return self._sha

    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes:
        self.fetches += 1
        return b"PK"


class _Sandbox:
    def __init__(self, report: SandboxReport) -> None:
        self._report = report
        self.calls = 0

    async def run(
        self,
        *,
        execution_id: str,
        selector: str,
        sha: str,
        source_zip: bytes,
        wall_seconds: int,
        memory_mb: int,
        pids: int,
        cancel: object = None,
    ) -> SandboxReport:
        del execution_id, selector, sha, wall_seconds, memory_mb, pids, cancel
        self.calls += 1
        self.zip = source_zip
        return self._report


class _Broken:
    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef:
        del owner, org_id, repo_id, data
        raise ArtifactStoreError("invalid_request")

    async def get(self, ref: object) -> bytes:
        del ref
        raise ArtifactStoreError("not_found")


async def _schedule(engine: AsyncEngine, host: _Host, selector: str) -> str:
    async with transaction(engine) as session:
        binding = new_binding(org_id=ORG, owner="acme", name=f"web-{selector[-6:]}")
        await SqlRepoDirectory(session).add(binding, now=NOW)
        record = await schedule(
            SqlRepoDirectory(session),
            host,
            SqlExecutionStore(session),
            access=OrgAccess(USER, ORG, org_admin=True),
            code_host=CodeHostAccess.READ,
            org_id=ORG,
            repo_id=binding.repo_id,
            selector=selector,
            claimed_sha=SHA,
            actor=Actor(ActorType.USER, USER),
            origin=Origin(Channel.CLI, f"corr-{selector[-6:]}"),
            now=NOW,
        )
    return record.id


def test_closed_mode_does_not_fetch_and_a_bad_result_is_not_a_pass(
    migrated_database_url: str, tmp_path: Path
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, int, str, int]:
        host = _Host(SHA)
        other = _Host(SHA)
        closed_id = await _schedule(engine, host, SELECTOR)
        await reproduce(
            engine,
            closed_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=ClosedSandbox(),
            host=host,
            artifacts=None,
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        bad = _Sandbox(
            SandboxReport(
                "finished",
                "finished",
                (
                    SandboxFile(
                        "result",
                        b'{"status":"passed","selector":"tests.login.TestLogin.test_bad","verified":true}',
                    ),
                ),
            )
        )
        bad_id = await _schedule(engine, other, "tests.login.TestLogin.test_bad")
        await reproduce(
            engine,
            bad_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=bad,
            host=other,
            artifacts=LocalFsArtifactStore(_root(tmp_path)),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        async with transaction(engine) as session:
            closed = await SqlExecutionStore(session).get(closed_id)
            invalid = await SqlExecutionStore(session).get(bad_id)
            assert closed is not None and invalid is not None
            rows = await session.execute(
                text(
                    "SELECT count(*) FROM exec.execution_artifacts "
                    "WHERE execution_id = :id"
                ),
                {"id": bad_id},
            )
            return (
                closed.reason or "",
                host.fetches,
                invalid.outcome or "",
                int(rows.scalar_one()),
            )

    reason, fetches, outcome, artifacts = run(migrated_database_url, scenario)
    assert reason == "execution_closed"
    assert fetches == 0
    assert outcome == "invalid_result"
    assert artifacts == 0


def test_a_pass_is_stored_for_the_row_org_and_a_second_run_is_a_no_op(
    migrated_database_url: str, tmp_path: Path
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, str, int, bool, int]:
        host = _Host(SHA)
        body = b'{"status":"passed","selector":"tests.login.TestLogin.test_pass"}'
        sandbox = _Sandbox(
            SandboxReport(
                "finished",
                "finished",
                (
                    SandboxFile("result", body),
                    SandboxFile("screenshot", PNG_MAGIC + b"png"),
                ),
            )
        )
        execution_id = await _schedule(engine, host, "tests.login.TestLogin.test_pass")
        root = _root(tmp_path)
        await reproduce(
            engine,
            execution_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=sandbox,
            host=host,
            artifacts=LocalFsArtifactStore(root),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        await reproduce(
            engine,
            execution_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=sandbox,
            host=host,
            artifacts=LocalFsArtifactStore(root),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        async with transaction(engine) as session:
            current = await SqlExecutionStore(session).get(execution_id)
            assert current is not None
            found = await session.execute(
                text(
                    "SELECT artifact_key FROM exec.execution_artifacts "
                    "WHERE execution_id = :id ORDER BY kind"
                ),
                {"id": execution_id},
            )
            rows = found.all()
            return (
                current.outcome or "",
                rows[0][0],
                len(rows),
                current.evidence_partial,
                sandbox.calls,
            )

    outcome, key, count, partial, calls = run(migrated_database_url, scenario)
    assert outcome == "success"
    assert key.startswith(f"exec/{ORG}/")
    assert count == 2
    assert partial is False
    assert calls == 1
    stored = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert stored
    assert all(b"verified" not in path.read_bytes() for path in stored)


def test_artifact_failure_still_records_the_outcome(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[str, bool]:
        host = _Host(SHA)
        sandbox = _Sandbox(
            SandboxReport(
                "finished",
                "finished",
                (
                    SandboxFile(
                        "result",
                        b'{"status":"failed","selector":"tests.login.TestLogin.test_part"}',
                    ),
                ),
            )
        )
        execution_id = await _schedule(engine, host, "tests.login.TestLogin.test_part")
        await reproduce(
            engine,
            execution_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=sandbox,
            host=host,
            artifacts=_Broken(),
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        async with transaction(engine) as session:
            current = await SqlExecutionStore(session).get(execution_id)
            assert current is not None
            return current.outcome or "", current.evidence_partial

    outcome, partial = run(migrated_database_url, scenario)
    assert outcome == "test_failure"
    assert partial is True


def test_timeout_infra_and_cancel_are_not_test_results(
    migrated_database_url: str,
) -> None:
    passed = b'{"status":"passed","selector":"tests.login.TestLogin.test_time","verified":true}'

    async def scenario(engine: AsyncEngine) -> tuple[str, str, int, int, str]:
        host = _Host(SHA)
        timed = _Sandbox(
            SandboxReport("timed_out", "timeout", (SandboxFile("result", passed),))
        )
        timed_id = await _schedule(engine, host, "tests.login.TestLogin.test_time")
        await reproduce(
            engine,
            timed_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=timed,
            host=host,
            artifacts=None,
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        failed_host = _Down(SHA)
        infra = _Sandbox(
            SandboxReport(
                "finished",
                "finished",
                (
                    SandboxFile(
                        "result",
                        b'{"status":"passed","selector":"tests.login.TestLogin.test_down"}',
                    ),
                ),
            )
        )
        infra_id = await _schedule(
            engine, failed_host, "tests.login.TestLogin.test_down"
        )
        await reproduce(
            engine,
            infra_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=infra,
            host=failed_host,
            artifacts=None,
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        stopped = _Sandbox(
            SandboxReport(
                "finished",
                "finished",
                (
                    SandboxFile(
                        "result",
                        b'{"status":"passed","selector":"tests.login.TestLogin.test_stop"}',
                    ),
                ),
            )
        )
        stop_host = _Host(SHA)
        stop_id = await _schedule(engine, stop_host, "tests.login.TestLogin.test_stop")
        async with transaction(engine) as session:
            await SqlExecutionStore(session).request_cancel(
                execution_id=stop_id, now=NOW
            )
        await reproduce(
            engine,
            stop_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=stopped,
            host=stop_host,
            artifacts=None,
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        async with transaction(engine) as session:
            records = SqlExecutionStore(session)
            timeout = await records.get(timed_id)
            broken = await records.get(infra_id)
            cancelled = await records.get(stop_id)
            assert timeout is not None and broken is not None and cancelled is not None
            rows = await session.execute(
                text(
                    "SELECT count(*) FROM exec.execution_artifacts "
                    "WHERE execution_id IN (:a, :b, :c)"
                ),
                {"a": timed_id, "b": infra_id, "c": stop_id},
            )
            return (
                timeout.outcome or "",
                broken.reason or "",
                infra.calls,
                int(rows.scalar_one()),
                cancelled.state,
            )

    outcome, reason, calls, artifacts, state = run(migrated_database_url, scenario)
    assert outcome == "timeout"
    assert reason == "source_unverified"
    assert calls == 0
    assert artifacts == 0
    assert state == "cancelled"


def test_restart_enqueues_only_the_execution_id(
    migrated_database_url: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[bool, str, str]:
        host = _Host(SHA)
        selector = "tests.login.TestLogin.test_wake"
        execution_id = await _schedule(engine, host, selector)
        await enqueue_missing(engine, NOW)
        async with transaction(engine) as session:
            current = await SqlExecutionStore(session).get(execution_id)
            assert current is not None and current.job_id is not None
            found = await session.execute(
                text(
                    "SELECT payload = CAST(:expected AS jsonb) "
                    "FROM orch.jobs WHERE id = :id"
                ),
                {
                    "id": current.job_id,
                    "expected": '{"execution_id":"' + execution_id + '"}',
                },
            )
            payload_ok = bool(found.scalar_one())
        await reproduce(
            engine,
            execution_id,
            owner="worker-a",
            now=NOW,
            lease=timedelta(seconds=30),
            sandbox=ClosedSandbox(),
            host=host,
            artifacts=None,
            wall_seconds=5,
            memory_mb=128,
            pids=16,
        )
        async with transaction(engine) as session:
            current = await SqlExecutionStore(session).get(execution_id)
            assert current is not None
            event = await session.execute(
                text(
                    "SELECT after, payload::text FROM exec.outbox "
                    "WHERE subject_id = :id AND name = 'execution.reproduction.finished'"
                ),
                {"id": execution_id},
            )
            after, payload = event.one()
            return payload_ok, current.outcome or "", f"{after} {payload}"

    payload_ok, outcome, marker = run(migrated_database_url, scenario)
    assert payload_ok is True
    assert outcome == "inconclusive"
    assert "inconclusive" in marker
    assert "test_wake" not in marker
    assert SHA not in marker


def test_a_rejected_finish_keeps_the_attempt_running(
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def reject(self: SqlExecutionStore, **kwargs: object) -> bool:
        del self, kwargs
        return False

    monkeypatch.setattr(SqlExecutionStore, "finish", reject)

    async def scenario(engine: AsyncEngine) -> tuple[str, int]:
        host = _Host(SHA)
        sandbox = _Sandbox(
            SandboxReport(
                "finished",
                "finished",
                (
                    SandboxFile(
                        "result",
                        b'{"status":"passed","selector":"tests.login.TestLogin.test_hold"}',
                    ),
                ),
            )
        )
        execution_id = await _schedule(engine, host, "tests.login.TestLogin.test_hold")
        with pytest.raises(RetryableJob):
            await reproduce(
                engine,
                execution_id,
                owner="worker-a",
                now=NOW,
                lease=timedelta(seconds=30),
                sandbox=sandbox,
                host=host,
                artifacts=LocalFsArtifactStore(_root(tmp_path)),
                wall_seconds=5,
                memory_mb=128,
                pids=16,
            )
        async with transaction(engine) as session:
            current = await SqlExecutionStore(session).get(execution_id)
            assert current is not None
            rows = await session.execute(
                text(
                    "SELECT count(*) FROM exec.execution_artifacts "
                    "WHERE execution_id = :id"
                ),
                {"id": execution_id},
            )
            return current.state, int(rows.scalar_one())

    state, artifacts = run(migrated_database_url, scenario)
    assert state == "running"
    assert artifacts == 0


class _Down(_Host):
    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes:
        del owner, name, sha
        self.fetches += 1
        raise CodeHostError("source_unverified")
