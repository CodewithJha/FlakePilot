"""Operator backfill of historical CI results with their original CI time (goal 12).

The prioritization evaluation needs history across several windows, but an upload is
stamped with its receipt time. A backfill imports one historical CI report that the
code host still holds, with explicit provenance: `ci_source` is
`github_actions_backfill`, the upload names the original run's ID, attempt and URL,
and the run carries the code host's completion time (`ci_completed_at`). Receipt
time stays the import time. Analytics and the evaluation read CI time
(`TiStore.repo_history`); classification evidence keeps receipt order.

Operator trust: the composition root resolves the (org, repo) pair to its registered
code-host binding before anything is fetched, so one repository's history can only
land in the pair bound to it. Idempotency is the upload key (org, repo,
`github_actions_backfill`, run ID, attempt, artifact name): the same key with the
same archive is a duplicate and writes nothing; the same key with another archive is
refused. Every outcome is one `ti.backfill.*` event with the operator as actor.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from flakepilot.integrations.application.ports import (
    ArtifactStore,
    ParsedTestCase,
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
    RunSummary,
    UnitOfWork,
)
from flakepilot.test_intelligence.domain.results import (
    BACKFILL_CI_SOURCE,
    HEADERS,
    InvalidMetadata,
    Outcome,
    RunSource,
    TooManyAttempts,
    UploadMetadata,
    merge_results,
    parse_metadata,
)

BACKFILL_SOURCE = BACKFILL_CI_SOURCE
ARCHIVE_CONTENT_TYPE = "application/zip"
ARTIFACT_OWNER = "ti"
MAX_URL_CHARS = 300


class BackfillStatus(StrEnum):
    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class HistoricalReport:
    """One CI run's report archive, as the code host describes the run."""

    ci_run_id: str
    ci_attempt: int
    job_name: str  # the artifact's name
    commit_sha: str
    ref: str
    environment: str
    started_at: datetime
    completed_at: datetime
    run_url: str


@dataclass(frozen=True)
class BackfillResult:
    status: BackfillStatus
    reason: str | None = None
    upload_id: str | None = None
    run_id: str | None = None
    tests: int = 0


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Backfill:
    def __init__(
        self, unit_of_work: UnitOfWork, artifacts: ArtifactStore, parser: ReportParser
    ) -> None:
        self._unit_of_work = unit_of_work
        self._artifacts = artifacts
        self._parser = parser

    async def import_report(
        self,
        *,
        org_id: str,
        repo_id: str,
        report: HistoricalReport,
        archive: bytes,
        documents: Sequence[bytes],
        origin: Origin,
    ) -> BackfillResult:
        """`archive` is stored as the raw evidence; `documents` are the XML
        files extracted from it. Documents that are not JUnit are ignored."""
        try:
            metadata = _metadata(report)
            cases = await self._parse(documents)
            results = merge_results((c.classname, c.name, c.attempts) for c in cases)
        except _Refused as refused:
            return await self.refuse(
                org_id=org_id,
                repo_id=repo_id,
                report=report,
                reason=refused.reason,
                origin=origin,
            )
        except TooManyAttempts:
            return await self.refuse(
                org_id=org_id,
                repo_id=repo_id,
                report=report,
                reason="too_many_attempts",
                origin=origin,
            )
        digest = hashlib.sha256(archive).digest()
        async with self._unit_of_work() as store:
            existing = await store.processed_upload(
                org_id=org_id, repo_id=repo_id, metadata=metadata
            )
        if existing is None:
            artifact = await self._artifacts.put(
                owner=ARTIFACT_OWNER, org_id=org_id, repo_id=repo_id, data=archive
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
            async with self._unit_of_work() as store:
                claimed = await store.claim_upload(
                    NewUpload(
                        upload_id=upload_id,
                        org_id=org_id,
                        repo_id=repo_id,
                        token_id=None,
                        metadata=metadata,
                        content_sha256=digest,
                        size_bytes=len(archive),
                        content_type=ARCHIVE_CONTENT_TYPE,
                        artifact_key=artifact.key,
                        provenance=BACKFILL_SOURCE,
                        origin_run_url=report.run_url,
                    )
                )
                if claimed:
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
                        ci_completed_at=report.completed_at,
                    )
                    await store.add_results(
                        org_id=org_id,
                        repo_id=repo_id,
                        run_id=run_id,
                        results=[(ids[r.identity], r) for r in results],
                    )
                    await store.append_event(
                        _event(
                            "ti.backfill.imported",
                            org_id,
                            repo_id,
                            upload_id,
                            origin,
                            after="processed",
                            payload={
                                **_provenance(report),
                                "run_id": run_id,
                                "tests": summary.tests,
                                "attempts": summary.attempts,
                            },
                        )
                    )
                    return BackfillResult(
                        BackfillStatus.IMPORTED,
                        upload_id=upload_id,
                        run_id=run_id,
                        tests=summary.tests,
                    )
            async with self._unit_of_work() as store:  # a concurrent import won
                existing = await store.processed_upload(
                    org_id=org_id, repo_id=repo_id, metadata=metadata
                )
            assert existing is not None
        if existing.content_sha256 != digest:
            return await self.refuse(
                org_id=org_id,
                repo_id=repo_id,
                report=report,
                reason="idempotency_conflict",
                origin=origin,
            )
        async with self._unit_of_work() as store:
            await store.append_event(
                _event(
                    "ti.backfill.duplicate",
                    org_id,
                    repo_id,
                    existing.upload_id,
                    origin,
                    payload={**_provenance(report), "run_id": existing.run_id},
                )
            )
        return BackfillResult(
            BackfillStatus.DUPLICATE,
            upload_id=existing.upload_id,
            run_id=existing.run_id,
            tests=existing.summary.tests,
        )

    async def refuse(
        self,
        *,
        org_id: str,
        repo_id: str,
        report: HistoricalReport,
        reason: str,
        origin: Origin,
    ) -> BackfillResult:
        """Record one audited refusal (nothing else is written)."""
        async with self._unit_of_work() as store:
            await store.append_event(
                _event(
                    "ti.backfill.rejected",
                    org_id,
                    repo_id,
                    str(uuid.uuid4()),
                    origin,
                    after="rejected",
                    reason=reason,
                    payload=_provenance(report),
                )
            )
        return BackfillResult(BackfillStatus.REJECTED, reason=reason)

    async def _parse(self, documents: Sequence[bytes]) -> list[ParsedTestCase]:
        cases: list[ParsedTestCase] = []
        for document in documents:
            try:
                cases.extend(await asyncio.to_thread(self._parser.parse, document))
            except ReportRejected as rejected:
                if rejected.reason != "not_junit":  # other XML in the artifact
                    raise _Refused(rejected.reason) from None
        if not cases:
            raise _Refused("no_test_cases")
        return cases


def _metadata(report: HistoricalReport) -> UploadMetadata:
    """The strict upload metadata rules, applied to what the code host reported."""
    url = report.run_url
    if not (
        url.startswith("https://")
        and len(url) <= MAX_URL_CHARS
        and url.isprintable()
        and " " not in url
    ):
        raise _Refused("invalid_metadata")
    values = {
        "ci_source": BACKFILL_SOURCE,
        "ci_run_id": report.ci_run_id,
        "ci_attempt": str(report.ci_attempt),
        "job_name": report.job_name,
        "commit_sha": report.commit_sha,
        "ref": report.ref,
        "environment": report.environment,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.completed_at.isoformat(),
    }
    try:
        return parse_metadata(
            {HEADERS[field]: value for field, value in values.items()}
        )
    except InvalidMetadata:
        raise _Refused("invalid_metadata") from None


def _provenance(report: HistoricalReport) -> dict[str, PayloadValue]:
    return {
        "source": BACKFILL_SOURCE,
        "ci_run_id": report.ci_run_id[:100],
        "ci_attempt": report.ci_attempt,
        "ci_completed_at": report.completed_at.isoformat()[:100],
    }


def _event(
    name: str,
    org_id: str,
    repo_id: str,
    upload_id: str,
    origin: Origin,
    *,
    after: str | None = None,
    reason: str | None = None,
    payload: Mapping[str, PayloadValue],
) -> Event:
    return Event(
        name,
        Actor(ActorType.OPERATOR_CLI),
        origin,
        Subject("upload", upload_id),
        org_id=org_id,
        repo_id=repo_id,
        after=after,
        reason=reason,
        payload=payload,
    )
