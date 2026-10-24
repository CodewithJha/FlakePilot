"""TI persistence port, used inside one unit of work. Implementations never commit."""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from flakepilot.shared_kernel.events import Event
from flakepilot.test_intelligence.domain.classification import Observation
from flakepilot.test_intelligence.domain.results import (
    RunSource,
    TestIdentity,
    TestResult,
    UploadMetadata,
)


@dataclass(frozen=True)
class RunSummary:
    tests: int
    attempts: int
    passed: int
    failed: int
    errored: int
    skipped: int


@dataclass(frozen=True)
class ProcessedUpload:
    upload_id: str
    run_id: str
    content_sha256: bytes
    summary: RunSummary


@dataclass(frozen=True)
class NewUpload:
    upload_id: str
    org_id: str
    repo_id: str
    token_id: str
    metadata: UploadMetadata
    content_sha256: bytes
    size_bytes: int
    content_type: str
    artifact_key: str


@dataclass(frozen=True)
class RejectedUpload:
    upload_id: str
    org_id: str
    repo_id: str
    token_id: str
    reason: str
    metadata: UploadMetadata | None
    content_sha256: bytes | None
    size_bytes: int | None


@dataclass(frozen=True)
class ReportedCase:
    """One test identity as a run reports it, attempts in order.

    `test_id` is the stored identity's ID in this (org, repo) pair: the key
    the classification evidence query reads with.
    """

    classname: str
    name: str
    attempts: tuple[str, ...]
    test_id: str


@dataclass(frozen=True)
class RunReport:
    """One stored run, read-only: the row, its upload metadata, its attempts.

    Operator-demo reporting only. A name is never evidence by itself; the
    revision status stays `unchecked` until a code-host verifier exists
    (ADR-013 deviation 4).
    """

    run_id: str
    org_id: str
    repo_id: str
    upload_id: str
    source: str
    commit_sha: str
    environment: str
    revision_status: str
    received_at: datetime | None
    summary: RunSummary
    metadata: UploadMetadata | None
    content_sha256: bytes | None
    size_bytes: int | None
    artifact_key: str | None
    results: tuple[ReportedCase, ...]


@dataclass(frozen=True)
class RunIndexEntry:
    """One stored run's lean identity for listings, read-only.

    Exactly the columns already stored for the run row; no classification is
    computed here (per-test verdicts belong to each run's own view)."""

    run_id: str
    source: str
    commit_sha: str
    environment: str
    revision_status: str
    received_at: datetime | None
    summary: RunSummary


class TiStore(Protocol):
    async def append_event(self, event: Event) -> None: ...

    async def claim_upload(self, upload: NewUpload) -> bool:
        """Insert as `processed` unless a processed upload has the same idempotency key;
        False (nothing written) if one does, after any concurrent claim has committed."""
        ...

    async def processed_upload(
        self, *, org_id: str, repo_id: str, metadata: UploadMetadata
    ) -> ProcessedUpload | None:
        """The processed upload with this idempotency key, if any."""
        ...

    async def record_rejection(self, upload: RejectedUpload) -> None: ...

    async def ensure_identities(
        self, *, org_id: str, repo_id: str, identities: Sequence[TestIdentity]
    ) -> dict[TestIdentity, str]: ...

    async def create_run(
        self,
        *,
        run_id: str,
        upload_id: str,
        org_id: str,
        repo_id: str,
        source: RunSource,
        commit_sha: str,
        environment: str,
        summary: RunSummary,
    ) -> None: ...

    async def add_results(
        self,
        *,
        org_id: str,
        repo_id: str,
        run_id: str,
        results: Sequence[tuple[str, TestResult]],
    ) -> None:
        """(test identity ID, result) pairs; one row per attempt."""
        ...

    async def run_repo_id(self, *, org_id: str, run_id: str) -> str | None:
        """The repo a stored run belongs to, or None if it is not in this org.
        The org-scoped lookup: a run in another org is simply not found."""
        ...

    async def observations(
        self, *, org_id: str, repo_id: str, test_id: str
    ) -> list[Observation] | None:
        """The identity's history, or None if it is not in this org and repo."""
        ...

    async def run_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> RunReport | None:
        """One run and its attempts, or None when it is not in this pair."""
        ...

    async def runs_index(
        self, *, org_id: str, repo_id: str, limit: int
    ) -> tuple[RunIndexEntry, ...]:
        """The pair's stored runs, newest first, at most `limit` rows.

        An (org, repo) pair with no stored runs is simply an empty tuple:
        existence of the pair is the caller's authorization question, not a
        listing answer."""
        ...


UnitOfWork = Callable[[], AbstractAsyncContextManager[TiStore]]
