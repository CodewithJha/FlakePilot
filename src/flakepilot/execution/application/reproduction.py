"""Schedule one browser reproduction. Authorization happens here, before any write.

The payload of the later job is only the execution id. This module does not
import a store implementation, and it does not run a sandbox.
"""

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from flakepilot.execution.domain.reproduction import (
    JOB_TYPE,
    Decision,
    born_inconclusive,
    born_scheduled,
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
__all__ = ["JOB_TYPE", "Execution", "ExecutionStore", "InvalidExecution", "schedule"]

_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,100}")


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


class ExecutionStore(Protocol):
    """Persistence for one execution. Runs inside the caller's transaction."""

    async def find_active(
        self, *, org_id: str, repo_id: str, selector: str, sha: str
    ) -> Execution | None: ...

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
