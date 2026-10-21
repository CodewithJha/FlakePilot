"""Accept a JUnit upload from a CI upload token (PRODUCT-SCOPE Workflow B; ADR-013).

The token's (org, repo) is the only scope; the client never names either. Every attempt is
recorded: accepted uploads in one transaction with their run, results and outbox events;
rejections as a `rejected` upload row plus `ti.upload.rejected`, in their own transaction.
Normalization runs synchronously inside the accepting transaction (ADR-013 deviation 2).

Idempotency key: (org, repo, CI source, CI run ID, attempt, job name), unique among
processed uploads in PostgreSQL. Same key and same SHA-256: the existing upload is
returned (`duplicate`) and nothing new is stored. Same key, different hash: rejected as
`idempotency_conflict`. Concurrent submissions resolve on the unique index.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from flakepilot.identity.application.iam import CiUploadPrincipal
from flakepilot.integrations.application.ports import (
    ArtifactStore,
    ReportParser,
    ReportRejected,
)
from flakepilot.shared_kernel.events import (
    Actor,
    ActorType,
    Event,
    Origin,
    PayloadValue,
    Subject,
)
from flakepilot.test_intelligence.application.store import (
    NewUpload,
    RejectedUpload,
    RunSummary,
    UnitOfWork,
)
from flakepilot.test_intelligence.domain.results import (
    HEADERS,
    InvalidMetadata,
    Outcome,
    RunSource,
    TestResult,
    TooManyAttempts,
    UploadMetadata,
    merge_results,
    parse_metadata,
)

_logger = logging.getLogger("flakepilot.ingestion")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # provisional (ADR-013); enforced while reading
CONTENT_TYPES = frozenset({"application/xml", "text/xml"})
METADATA_HEADERS = tuple(HEADERS.values())
ARTIFACT_OWNER = "ti"

TOO_LARGE = "too_large"
UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
INVALID_METADATA = "invalid_metadata"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class IngestionNotConfigured(Exception):
    """No artifact storage is configured, so uploads cannot be accepted (fail closed)."""


class UploadRejected(Exception):
    """A recorded, audited rejection. `reason` is machine-readable; `header` names an
    invalid metadata header. Never carries submitted values."""

    def __init__(self, reason: str, header: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.header = header


@dataclass(frozen=True)
class IngestionResult:
    upload_id: str
    run_id: str
    duplicate: bool
    summary: RunSummary


@dataclass(frozen=True)
class _Attempt:
    """What is known about one submission, for its audit record."""

    principal: CiUploadPrincipal
    origin: Origin
    digest: bytes | None
    size: int | None

    def event(
        self,
        name: str,
        upload_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
        reason: str | None = None,
        payload: Mapping[str, PayloadValue] | None = None,
    ) -> Event:
        return Event(
            name,
            Actor(ActorType.CI_TOKEN, self.principal.token_id),
            self.origin,
            Subject("upload", upload_id),
            org_id=self.principal.org_id,
            repo_id=self.principal.repo_id,
            before=before,
            after=after,
            reason=reason,
            payload=payload
            if payload is not None
            else {  # identifiers of the submitted bytes, never content
                "content_sha256": self.digest.hex() if self.digest else None,
                "size_bytes": self.size,
            },
        )

    def rejection(
        self, upload_id: str, reason: str, metadata: UploadMetadata | None
    ) -> RejectedUpload:
        return RejectedUpload(
            upload_id=upload_id,
            org_id=self.principal.org_id,
            repo_id=self.principal.repo_id,
            token_id=self.principal.token_id,
            reason=reason,
            metadata=metadata,
            content_sha256=self.digest,
            size_bytes=self.size,
        )


class Ingestion:
    def __init__(
        self, unit_of_work: UnitOfWork, artifacts: ArtifactStore, parser: ReportParser
    ) -> None:
        self._unit_of_work = unit_of_work
        self._artifacts = artifacts
        self._parser = parser

    async def ingest(
        self,
        principal: CiUploadPrincipal,
        *,
        headers: Mapping[str, str],
        content_type: str | None,
        content_encoding: str | None,
        body: bytes | None,
        origin: Origin,
    ) -> IngestionResult:
        """`body` is None when it exceeded `MAX_UPLOAD_BYTES` (reading stopped)."""
        attempt = _Attempt(
            principal,
            origin,
            hashlib.sha256(body).digest() if body is not None else None,
            len(body) if body is not None else None,
        )
        metadata: UploadMetadata | None = None
        try:
            if body is None:
                raise UploadRejected(TOO_LARGE)
            media_type = (content_type or "").split(";", 1)[0].strip().lower()
            if media_type not in CONTENT_TYPES or content_encoding not in (
                None,
                "identity",
            ):
                raise UploadRejected(UNSUPPORTED_MEDIA_TYPE)
            try:
                metadata = parse_metadata(headers)
            except InvalidMetadata as exc:
                raise UploadRejected(INVALID_METADATA, exc.header) from None
            try:
                parsed = await asyncio.to_thread(self._parser.parse, body)
                results = merge_results(
                    (c.classname, c.name, c.attempts) for c in parsed
                )
            except ReportRejected as exc:
                raise UploadRejected(exc.reason) from None
            except TooManyAttempts:
                raise UploadRejected("too_many_attempts") from None
        except UploadRejected as rejection:
            _logger.warning("upload rejected: %s", rejection.reason)
            async with self._unit_of_work() as store:
                upload_id = str(uuid.uuid4())
                await store.record_rejection(
                    attempt.rejection(upload_id, rejection.reason, metadata)
                )
                await store.append_event(
                    attempt.event(
                        "ti.upload.rejected",
                        upload_id,
                        after="rejected",
                        reason=rejection.reason,
                    )
                )
            raise
        assert metadata is not None and body is not None
        return await self._accept(attempt, metadata, media_type, body, results)

    async def _accept(
        self,
        attempt: _Attempt,
        metadata: UploadMetadata,
        media_type: str,
        body: bytes,
        results: list[TestResult],
    ) -> IngestionResult:
        principal = attempt.principal
        org_id, repo_id = principal.org_id, principal.repo_id
        artifact = await self._artifacts.put(
            owner=ARTIFACT_OWNER, org_id=org_id, repo_id=repo_id, data=body
        )
        finals = [r.final for r in results]
        summary = RunSummary(
            tests=len(results),
            attempts=sum(len(r.attempts) for r in results),
            passed=finals.count(Outcome.PASSED),
            failed=finals.count(Outcome.FAILED),
            errored=finals.count(Outcome.ERRORED),
            skipped=finals.count(Outcome.SKIPPED),
        )
        upload_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        digest = hashlib.sha256(body).digest()
        conflict: str | None = None
        async with self._unit_of_work() as store:
            claimed = await store.claim_upload(
                NewUpload(
                    upload_id=upload_id,
                    org_id=org_id,
                    repo_id=repo_id,
                    token_id=principal.token_id,
                    metadata=metadata,
                    content_sha256=digest,
                    size_bytes=len(body),
                    content_type=media_type,
                    artifact_key=artifact.key,
                )
            )
            if not claimed:
                existing = await store.processed_upload(
                    org_id=org_id, repo_id=repo_id, metadata=metadata
                )
                if existing.content_sha256 == digest:
                    await store.append_event(
                        attempt.event("ti.upload.duplicate", existing.upload_id)
                    )
                    return IngestionResult(
                        existing.upload_id, existing.run_id, True, existing.summary
                    )
                conflict = str(uuid.uuid4())
                await store.record_rejection(
                    attempt.rejection(conflict, IDEMPOTENCY_CONFLICT, metadata)
                )
                await store.append_event(
                    attempt.event(
                        "ti.upload.rejected",
                        conflict,
                        after="rejected",
                        reason=IDEMPOTENCY_CONFLICT,
                    )
                )
            else:
                await store.append_event(
                    attempt.event("ti.upload.received", upload_id, after="received")
                )
                ids = await store.ensure_identities(
                    org_id=org_id,
                    repo_id=repo_id,
                    identities=[r.identity for r in results],
                )
                await store.create_run(
                    run_id=run_id,
                    upload_id=upload_id,
                    org_id=org_id,
                    repo_id=repo_id,
                    source=RunSource.CI_UPLOAD,
                    commit_sha=metadata.commit_sha,
                    environment=metadata.environment,
                    summary=summary,
                )
                await store.add_results(
                    org_id=org_id,
                    repo_id=repo_id,
                    run_id=run_id,
                    results=[(ids[r.identity], r) for r in results],
                )
                await store.append_event(
                    attempt.event(
                        "ti.upload.processed",
                        upload_id,
                        before="received",
                        after="processed",
                        payload={
                            "run_id": run_id,
                            "tests": summary.tests,
                            "attempts": summary.attempts,
                        },
                    )
                )
        if conflict is not None:
            _logger.warning("upload rejected: %s", IDEMPOTENCY_CONFLICT)
            raise UploadRejected(IDEMPOTENCY_CONFLICT)
        return IngestionResult(upload_id, run_id, False, summary)
