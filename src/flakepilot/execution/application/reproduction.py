"""Schedule one browser reproduction. Authorization happens here, before any write.

The payload of the later job is only the execution id. This module does not
import a store implementation, and it does not run a sandbox.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from flakepilot.execution.domain.evidence import (
    EvidenceVerdict,
    IsolationChecks,
    accept,
    unavailable,
)
from flakepilot.execution.domain.reproduction import (
    JOB_TYPE,
    Decision,
    born_inconclusive,
    born_scheduled,
    classify,
    screenshot_ok,
    selector_ok,
    sha_ok,
)
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    ResourceNotFound,
    authorize,
)
from flakepilot.integrations.application.ports import CodeHost
from flakepilot.repository.application.source import (
    RepoDirectory,
    SourceError,
    pin_revision,
)
from flakepilot.shared_kernel.events import Actor, Origin

# Re-exported for the composition root. The job type is not a request field.
__all__ = [
    "JOB_TYPE",
    "Decision",
    "EvidenceRef",
    "EvidenceVerdict",
    "Execution",
    "ExecutionStore",
    "InvalidExecution",
    "IsolationChecks",
    "IsolationProvider",
    "UnavailableIsolation",
    "UnverifiedCallerAccess",
    "accept",
    "classify",
    "evidence_of",
    "inspect",
    "record_closed",
    "schedule",
    "schedule_registered",
    "screenshot_ok",
    "unavailable",
]

_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,100}")


@dataclass(frozen=True)
class EvidenceRef:
    """A stored artifact the caller may name. Not the bytes."""

    kind: str
    key: str
    size: int


class IsolationProvider(Protocol):
    """Host facts for one execution. Production has no implementation that passes."""

    def current(self) -> IsolationChecks: ...


class UnavailableIsolation:
    """The only production attestation. Every required check is false."""

    def current(self) -> IsolationChecks:
        return unavailable()


class CallerAccess(Protocol):
    """Whether this caller may read that repository at the code host right now."""

    async def confirmed_read(self, owner: str, name: str) -> bool: ...


class UnverifiedCallerAccess:
    """No live code-host ACL was checked. Scheduling must not fetch source."""

    async def confirmed_read(self, owner: str, name: str) -> bool:
        del owner, name
        return False


class InvalidExecution(ValueError):
    """The request was rejected before a row was written. `reason` is a short code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class Execution:
    """One durable reproduction. `sha` on an inconclusive row is not verified evidence."""

    __slots__ = (
        "cancel_requested",
        "correlation_id",
        "evidence_partial",
        "generation",
        "id",
        "job_id",
        "lease_expires_at",
        "lease_owner",
        "org_id",
        "outcome",
        "reason",
        "repo_id",
        "selector",
        "sha",
        "state",
    )

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.id = str(row["id"])
        self.org_id = str(row["org_id"])
        self.repo_id = str(row["repo_id"])
        self.selector = str(row["selector"])
        self.sha = str(row["sha"])
        self.state = str(row["state"])
        self.outcome = None if row["outcome"] is None else str(row["outcome"])
        self.reason = None if row["reason"] is None else str(row["reason"])
        self.generation = int(str(row["generation"]))
        self.job_id = None if row["job_id"] is None else str(row["job_id"])
        self.evidence_partial = bool(row["evidence_partial"])
        self.cancel_requested = bool(row["cancel_requested"])
        self.lease_owner = (
            None if row["lease_owner"] is None else str(row["lease_owner"])
        )
        self.lease_expires_at = row["lease_expires_at"]
        self.correlation_id = str(row["correlation_id"])


def evidence_of(
    record: Execution,
    provider: IsolationProvider | None = None,
    *,
    superseded: bool = False,
) -> EvidenceVerdict:
    """Eligibility of one row. Omitted provider means the checks were not run."""
    checks = (
        UnavailableIsolation().current() if provider is None else provider.current()
    )
    return accept(
        outcome=record.outcome,
        state=record.state,
        reason=record.reason,
        sha=record.sha,
        org_id=record.org_id,
        repo_id=record.repo_id,
        evidence_partial=record.evidence_partial,
        superseded=superseded,
        checks=checks,
    )


class ExecutionStore(Protocol):
    """Persistence for one execution. Runs inside the caller's transaction."""

    async def find_active(
        self, *, org_id: str, repo_id: str, selector: str, sha: str
    ) -> Execution | None: ...

    async def find_closed(
        self, *, org_id: str, repo_id: str, selector: str, sha: str
    ) -> Execution | None: ...

    async def find_unverified(
        self, *, org_id: str, repo_id: str, selector: str, sha: str
    ) -> Execution | None: ...

    async def artifacts(self, execution_id: str) -> list[tuple[str, str, int]]: ...

    async def insert(
        self,
        *,
        org_id: str,
        repo_id: str,
        selector: str,
        sha: str,
        decision: Decision,
        actor: Actor,
        origin: Origin,
        now: datetime,
    ) -> Execution: ...

    async def get(self, execution_id: str) -> Execution | None: ...

    async def claim(
        self,
        *,
        execution_id: str,
        owner: str,
        now: datetime,
        lease: timedelta,
    ) -> Execution | None: ...

    async def finish(
        self,
        *,
        execution_id: str,
        owner: str,
        generation: int,
        now: datetime,
        decision: Decision,
        evidence_partial: bool,
        actor: Actor,
        origin: Origin,
    ) -> bool: ...

    async def request_cancel(self, *, execution_id: str, now: datetime) -> bool: ...

    async def attach_job(self, *, execution_id: str, job_id: str) -> bool: ...

    async def scheduled_without_job(self, limit: int) -> list[str]: ...


def _token(value: str) -> bool:
    return _TOKEN.fullmatch(value) is not None


async def schedule(
    directory: RepoDirectory,
    host: CodeHost,
    records: ExecutionStore,
    *,
    access: OrgAccess,
    code_host: CodeHostAccess,
    org_id: str,
    repo_id: str,
    selector: str,
    claimed_sha: str,
    actor: Actor,
    origin: Origin,
    now: datetime,
) -> Execution:
    """Authorize, pin, and insert. An unverified SHA is stored as inconclusive.

    A second schedule of the same active tuple returns the existing row.
    """
    if now.tzinfo is None or not _token(origin.correlation_id):
        raise InvalidExecution("invalid_request")
    authorize(
        access, Grant.INVESTIGATE, org_id=org_id, repo_id=repo_id, code_host=code_host
    )
    if not selector_ok(selector) or not sha_ok(claimed_sha):
        raise InvalidExecution("invalid_request")
    try:
        pinned = await pin_revision(
            directory,
            host,
            org_id=org_id,
            repo_id=repo_id,
            claimed_sha=claimed_sha,
        )
    except SourceError as exc:
        if exc.reason == "not_found":
            raise ResourceNotFound from None
        if exc.reason != "source_unverified":
            raise InvalidExecution(exc.reason) from None
        return await records.insert(
            org_id=org_id,
            repo_id=repo_id,
            selector=selector,
            sha=claimed_sha,
            decision=born_inconclusive("source_unverified"),
            actor=actor,
            origin=origin,
            now=now,
        )
    existing = await records.find_active(
        org_id=org_id, repo_id=repo_id, selector=selector, sha=pinned.sha
    )
    if existing is not None:
        return existing
    return await records.insert(
        org_id=org_id,
        repo_id=repo_id,
        selector=selector,
        sha=pinned.sha,
        decision=born_scheduled(),
        actor=actor,
        origin=origin,
        now=now,
    )


async def schedule_registered(
    directory: RepoDirectory,
    host: CodeHost,
    records: ExecutionStore,
    *,
    access: OrgAccess,
    org_id: str,
    repo_id: str,
    selector: str,
    claimed_sha: str,
    actor: Actor,
    origin: Origin,
    now: datetime,
    caller: CallerAccess | None = None,
) -> Execution:
    """Schedule one registered repository.

    The default caller has not confirmed a live code-host ACL. That path
    stores `source_unverified` and does not call the code host. A binding
    plus a FlakePilot grant is not that confirmation.
    """
    if now.tzinfo is None or not _token(origin.correlation_id):
        raise InvalidExecution("invalid_request")
    authorize(
        access,
        Grant.INVESTIGATE,
        org_id=org_id,
        repo_id=repo_id,
        code_host=CodeHostAccess.READ,
    )
    if not selector_ok(selector) or not sha_ok(claimed_sha):
        raise InvalidExecution("invalid_request")
    binding = await directory.get(repo_id)
    if binding is None or binding.org_id != org_id:
        raise ResourceNotFound
    checker = UnverifiedCallerAccess() if caller is None else caller
    if not await checker.confirmed_read(binding.owner, binding.name):
        existing = await records.find_unverified(
            org_id=org_id, repo_id=repo_id, selector=selector, sha=claimed_sha
        )
        if existing is not None:
            return existing
        return await records.insert(
            org_id=org_id,
            repo_id=repo_id,
            selector=selector,
            sha=claimed_sha,
            decision=born_inconclusive("source_unverified"),
            actor=actor,
            origin=origin,
            now=now,
        )
    return await schedule(
        directory,
        host,
        records,
        access=access,
        code_host=CodeHostAccess.READ,
        org_id=org_id,
        repo_id=repo_id,
        selector=selector,
        claimed_sha=claimed_sha,
        actor=actor,
        origin=origin,
        now=now,
    )


async def record_closed(
    directory: RepoDirectory,
    records: ExecutionStore,
    *,
    access: OrgAccess,
    org_id: str,
    repo_id: str,
    selector: str,
    claimed_sha: str,
    actor: Actor,
    origin: Origin,
    now: datetime,
) -> tuple[Execution, bool]:
    """Closed mode. No code host and no job. A repeat returns the same row."""
    if now.tzinfo is None or not _token(origin.correlation_id):
        raise InvalidExecution("invalid_request")
    authorize(
        access,
        Grant.INVESTIGATE,
        org_id=org_id,
        repo_id=repo_id,
        code_host=CodeHostAccess.READ,
    )
    if not selector_ok(selector) or not sha_ok(claimed_sha):
        raise InvalidExecution("invalid_request")
    binding = await directory.get(repo_id)
    if binding is None or binding.org_id != org_id:
        raise ResourceNotFound
    existing = await records.find_closed(
        org_id=org_id, repo_id=repo_id, selector=selector, sha=claimed_sha
    )
    if existing is not None:
        return existing, False
    created = await records.insert(
        org_id=org_id,
        repo_id=repo_id,
        selector=selector,
        sha=claimed_sha,
        decision=born_inconclusive("execution_closed"),
        actor=actor,
        origin=origin,
        now=now,
    )
    return created, created.correlation_id == origin.correlation_id


async def inspect(
    records: ExecutionStore,
    *,
    access: OrgAccess,
    org_id: str,
    execution_id: str,
) -> tuple[Execution, tuple[EvidenceRef, ...]]:
    """Status and artifact names. Missing, or not visible in this org, is not found."""
    current = await records.get(execution_id)
    if current is None or current.org_id != org_id:
        raise ResourceNotFound
    authorize(
        access,
        Grant.VIEW,
        org_id=org_id,
        repo_id=current.repo_id,
        code_host=CodeHostAccess.READ,
    )
    refs = tuple(
        EvidenceRef(kind, key, size)
        for kind, key, size in await records.artifacts(execution_id)
    )
    return current, refs
