"""TI persistence port, used inside one unit of work. Implementations never commit."""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
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

    async def observations(
        self, *, org_id: str, repo_id: str, test_id: str
    ) -> list[Observation] | None:
        """The identity's history, or None if it is not in this org and repo."""
        ...


UnitOfWork = Callable[[], AbstractAsyncContextManager[TiStore]]
