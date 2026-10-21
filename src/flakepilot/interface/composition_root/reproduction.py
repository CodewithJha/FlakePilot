"""Run one claimed reproduction and persist the control-plane outcome.

The job payload may contain only `execution_id`. The org, repo, selector and
SHA come from the execution row. Closed mode does not fetch source and does
not start a runtime.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.execution.application.reproduction import (
    JOB_TYPE,
    Decision,
    Execution,
    classify,
    screenshot_ok,
)
from flakepilot.execution.infrastructure.store import SqlExecutionStore
from flakepilot.integrations.infrastructure.artifact_store import (
    ArtifactStore,
    ArtifactStoreError,
    LocalFsArtifactStore,
)
from flakepilot.integrations.infrastructure.code_host import (
    CodeHost,
    CodeHostError,
    GitHubCodeHost,
)
from flakepilot.integrations.infrastructure.sandbox import (
    ClosedSandbox,
    ContainerSandbox,
    Sandbox,
    SandboxReport,
    unverified_source,
)
from flakepilot.interface.composition_root.database import transaction
from flakepilot.interface.composition_root.settings import Settings
from flakepilot.orchestration.application.jobs import Job, PermanentJob, RetryableJob
from flakepilot.orchestration.application.worker import Handler
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Actor, ActorType, Channel, Origin

_logger = logging.getLogger("flakepilot.worker")
_Evidence = tuple[str, str, bytes, int]


def reproduction_handlers(
    engine: AsyncEngine,
    settings: Settings,
    *,
    sandbox: Sandbox | None = None,
    host: CodeHost | None = None,
    artifacts: ArtifactStore | None = None,
    now: datetime | None = None,
) -> dict[str, Handler]:
    """The one registered job type. Tests may pass fakes; production does not."""
    chosen = sandbox if sandbox is not None else _sandbox(settings)
    code_host = host if host is not None else _host(settings)
    blobs = _artifacts(settings) if artifacts is None else artifacts

    async def handle(job: Job) -> None:
        execution_id = _execution_id(job)
        if job.lease_owner is None:
            raise PermanentJob("invalid_payload")
        await reproduce(
            engine,
            execution_id,
            owner=job.lease_owner,
            now=datetime.now(UTC) if now is None else now,
            lease=timedelta(seconds=settings.execution_wall_seconds + 30),
            sandbox=chosen,
            host=code_host,
            artifacts=blobs,
            wall_seconds=settings.execution_wall_seconds,
            memory_mb=settings.execution_memory_mb,
            pids=settings.execution_pids,
        )

    return {JOB_TYPE: handle}


def _execution_id(job: Job) -> str:
    if set(job.payload) != {"execution_id"}:
        raise PermanentJob("invalid_payload")
    execution_id = job.payload["execution_id"]
    if not isinstance(execution_id, str):
        raise PermanentJob("invalid_payload")
    return execution_id


async def reproduce(
    engine: AsyncEngine,
    execution_id: str,
    *,
    owner: str,
    now: datetime,
    lease: timedelta,
    sandbox: Sandbox,
    host: CodeHost,
    artifacts: ArtifactStore | None,
    wall_seconds: int,
    memory_mb: int,
    pids: int,
) -> None:
    async with transaction(engine) as session:
        records = SqlExecutionStore(session)
        current = await records.get(execution_id)
        if current is None:
            raise PermanentJob("invalid_payload")
        if current.state in ("completed", "cancelled"):
            return
        claimed = await records.claim(
            execution_id=execution_id, owner=owner, now=now, lease=lease
        )
    if claimed is None:
        async with transaction(engine) as session:
            again = await SqlExecutionStore(session).get(execution_id)
        if again is None:
            raise PermanentJob("invalid_payload")
        if again.state in ("completed", "cancelled"):
            return
        raise RetryableJob("busy")
    if claimed.state != "running":
        return
    report = await _execute(
        engine, claimed, sandbox, host, wall_seconds, memory_mb, pids
    )
    decision = classify(
        report.status,
        report.reason,
        _result_bytes(report),
        selector=claimed.selector,
    )
    saved, partial = await _evidence(artifacts, claimed, decision, report)
    async with transaction(engine) as session:
        records = SqlExecutionStore(session)
        for kind, key, digest, size in saved:
            await records.add_artifact(
                execution_id=claimed.id,
                kind=kind,
                digest=digest,
                size=size,
                artifact_key=key,
            )
        wrote = await records.finish(
            execution_id=claimed.id,
            owner=owner,
            generation=claimed.generation,
            now=now,
            decision=decision,
            evidence_partial=partial,
            actor=Actor(ActorType.SYSTEM),
            origin=Origin(Channel.CLI, claimed.correlation_id),
        )
        if not wrote:
            raise RetryableJob("stale_lease")
    _logger.info("execution finished: %s %s", claimed.id, decision.outcome)


async def _execute(
    engine: AsyncEngine,
    claimed: Execution,
    sandbox: Sandbox,
    host: CodeHost,
    wall_seconds: int,
    memory_mb: int,
    pids: int,
) -> SandboxReport:
    if isinstance(sandbox, ClosedSandbox):
        return await sandbox.run(
            execution_id=claimed.id,
            selector=claimed.selector,
            sha=claimed.sha,
            source_zip=b"",
            wall_seconds=wall_seconds,
            memory_mb=memory_mb,
            pids=pids,
        )
    async with transaction(engine) as session:
        binding = await SqlRepoDirectory(session).get(claimed.repo_id)
    if binding is None or binding.org_id != claimed.org_id:
        return unverified_source()
    try:
        blob = await host.fetch_archive(
            owner=binding.owner, name=binding.name, sha=claimed.sha
        )
    except CodeHostError as exc:
        _logger.info("execution source: %s %s", claimed.id, exc.reason)
        return unverified_source()
    return await sandbox.run(
        execution_id=claimed.id,
        selector=claimed.selector,
        sha=claimed.sha,
        source_zip=blob,
        wall_seconds=wall_seconds,
        memory_mb=memory_mb,
        pids=pids,
    )


def _result_bytes(report: SandboxReport) -> bytes | None:
    for item in report.files:
        if item.kind == "result":
            return item.data
    return None


async def _evidence(
    artifacts: ArtifactStore | None,
    claimed: Execution,
    decision: Decision,
    report: SandboxReport,
) -> tuple[list[_Evidence], bool]:
    """Store the canonical result, never the sandbox's own JSON."""
    partial = False
    pending: list[tuple[str, bytes]] = []
    if decision.outcome in ("success", "test_failure"):
        status = "passed" if decision.outcome == "success" else "failed"
        pending.append(
            (
                "result",
                json.dumps(
                    {"selector": claimed.selector, "status": status},
                    separators=(",", ":"),
                ).encode(),
            )
        )
        for item in report.files:
            if item.kind != "screenshot":
                continue
            if screenshot_ok(item.data):
                pending.append(("screenshot", item.data))
            else:
                partial = True
    if not pending:
        return [], partial
    if artifacts is None:
        return [], True
    saved: list[_Evidence] = []
    for kind, data in pending:
        try:
            ref = await artifacts.put(
                owner="exec",
                org_id=claimed.org_id,
                repo_id=claimed.repo_id,
                data=data,
            )
        except ArtifactStoreError:
            partial = True
            continue
        saved.append((kind, ref.key, bytes.fromhex(ref.sha256), ref.size))
    return saved, partial


def _sandbox(settings: Settings) -> Sandbox:
    if settings.execution_mode != "evaluation" or settings.execution_image is None:
        return ClosedSandbox()
    return ContainerSandbox(
        runtime=settings.execution_runtime, image=settings.execution_image
    )


def _host(settings: Settings) -> GitHubCodeHost:
    token = (
        None
        if settings.github_contents_token is None
        else settings.github_contents_token.reveal()
    )
    return GitHubCodeHost(token)


def _artifacts(settings: Settings) -> LocalFsArtifactStore | None:
    if settings.artifact_dir is None:
        return None
    return LocalFsArtifactStore(settings.artifact_dir)
