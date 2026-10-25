"""TI's PostgreSQL store. Runs inside a caller's transaction and never commits.

Every statement is scoped by (org_id, repo_id). Times come from PostgreSQL (`now()`).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import LargeBinary, any_, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.shared_kernel.events import Event
from flakepilot.test_intelligence.application.store import (
    IdentityHistory,
    NewUpload,
    ProcessedUpload,
    RejectedUpload,
    ReportedCase,
    RunIndexEntry,
    RunReport,
    RunSummary,
    TrendBucket,
)
from flakepilot.test_intelligence.domain.classification import Observation
from flakepilot.test_intelligence.domain.results import (
    Outcome,
    RunSource,
    TestIdentity,
    TestResult,
    UploadMetadata,
)
from flakepilot.test_intelligence.infrastructure.tables import (
    KEY_COLUMNS,
    outbox,
    runs,
    test_identities,
    test_results,
    uploads,
)

# A run's CI time: the original completion time of a backfilled run, else receipt.
_CI_TIME = func.coalesce(runs.c.ci_completed_at, runs.c.received_at)


def _metadata_columns(metadata: UploadMetadata | None) -> dict[str, object]:
    if metadata is None:
        return {}
    return {
        "ci_source": metadata.ci_source,
        "ci_run_id": metadata.ci_run_id,
        "ci_attempt": metadata.ci_attempt,
        "job_name": metadata.job_name,
        "commit_sha": metadata.commit_sha,
        "ref": metadata.ref,
        "environment": metadata.environment,
        "started_at": metadata.started_at,
        "finished_at": metadata.finished_at,
    }


class SqlTiStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_event(self, event: Event) -> None:
        await self._session.execute(
            outbox.insert().values(
                event_id=event.event_id,
                name=event.name,
                schema_version=event.schema_version,
                occurred_at=func.now(),
                org_id=event.org_id,
                repo_id=event.repo_id,
                actor_type=event.actor.type.value,
                actor_id=event.actor.id,
                channel=event.origin.channel.value,
                correlation_id=event.origin.correlation_id,
                subject_type=event.subject.type,
                subject_id=event.subject.id,
                before=event.before,
                after=event.after,
                reason=event.reason,
                payload=dict(event.payload),
            )
        )

    async def claim_upload(self, upload: NewUpload) -> bool:
        claimed = await self._session.execute(
            insert(uploads)
            .values(
                id=upload.upload_id,
                org_id=upload.org_id,
                repo_id=upload.repo_id,
                token_id=upload.token_id,
                state="processed",
                content_sha256=upload.content_sha256,
                size_bytes=upload.size_bytes,
                content_type=upload.content_type,
                artifact_key=upload.artifact_key,
                received_at=func.now(),
                provenance=upload.provenance,
                origin_run_url=upload.origin_run_url,
                **_metadata_columns(upload.metadata),
            )
            .on_conflict_do_nothing(
                index_elements=list(KEY_COLUMNS),
                index_where=text("state <> 'rejected'"),
            )
            .returning(uploads.c.id)
        )
        return claimed.first() is not None

    async def processed_upload(
        self, *, org_id: str, repo_id: str, metadata: UploadMetadata
    ) -> ProcessedUpload | None:
        key = _metadata_columns(metadata)
        found = await self._session.execute(
            select(
                uploads.c.id,
                uploads.c.content_sha256,
                runs.c.id.label("run_id"),
                runs.c.tests,
                runs.c.attempts,
                runs.c.passed,
                runs.c.failed,
                runs.c.errored,
                runs.c.skipped,
            )
            .join(runs, runs.c.upload_id == uploads.c.id)
            .where(
                uploads.c.org_id == org_id,
                uploads.c.repo_id == repo_id,
                uploads.c.state == "processed",
                *(uploads.c[name] == key[name] for name in KEY_COLUMNS[2:]),
            )
        )
        row = found.one_or_none()
        if row is None:
            return None
        return ProcessedUpload(
            row.id,
            row.run_id,
            bytes(row.content_sha256),
            RunSummary(
                row.tests,
                row.attempts,
                row.passed,
                row.failed,
                row.errored,
                row.skipped,
            ),
        )

    async def record_rejection(self, upload: RejectedUpload) -> None:
        await self._session.execute(
            uploads.insert().values(
                id=upload.upload_id,
                org_id=upload.org_id,
                repo_id=upload.repo_id,
                token_id=upload.token_id,
                state="rejected",
                reject_reason=upload.reason,
                content_sha256=upload.content_sha256,
                size_bytes=upload.size_bytes,
                received_at=func.now(),
                **_metadata_columns(upload.metadata),
            )
        )

    async def ensure_identities(
        self, *, org_id: str, repo_id: str, identities: Sequence[TestIdentity]
    ) -> dict[TestIdentity, str]:
        if not identities:
            return {}
        by_key = {identity.key: identity for identity in identities}
        rows = [  # sorted, so concurrent uploads lock new keys in the same order
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "repo_id": repo_id,
                "identity_key": key,
                "classname": identity.classname,
                "name": identity.name,
            }
            for key, identity in sorted(by_key.items())
        ]
        await self._session.execute(
            insert(test_identities)
            .values(created_at=func.now())
            .on_conflict_do_nothing(
                index_elements=["org_id", "repo_id", "identity_key"]
            ),
            rows,
        )
        found = await self._session.execute(
            select(test_identities.c.id, test_identities.c.identity_key).where(
                test_identities.c.org_id == org_id,
                test_identities.c.repo_id == repo_id,
                test_identities.c.identity_key
                == any_(bindparam("keys", list(by_key), type_=ARRAY(LargeBinary))),
            )
        )
        return {by_key[bytes(row.identity_key)]: row.id for row in found}

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
        ci_completed_at: datetime | None = None,
    ) -> None:
        await self._session.execute(
            runs.insert().values(
                ci_completed_at=ci_completed_at,
                id=run_id,
                org_id=org_id,
                repo_id=repo_id,
                upload_id=upload_id,
                source=source.value,
                commit_sha=commit_sha,
                environment=environment,
                revision_status="unchecked",  # ADR-013 deviation 4
                received_at=func.now(),
                tests=summary.tests,
                attempts=summary.attempts,
                passed=summary.passed,
                failed=summary.failed,
                errored=summary.errored,
                skipped=summary.skipped,
            )
        )

    async def add_results(
        self,
        *,
        org_id: str,
        repo_id: str,
        run_id: str,
        results: Sequence[tuple[str, TestResult]],
    ) -> None:
        rows = [
            {
                "org_id": org_id,
                "repo_id": repo_id,
                "run_id": run_id,
                "test_id": test_id,
                "attempt": number,
                "outcome": outcome.value,
            }
            for test_id, result in results
            for number, outcome in enumerate(result.attempts, start=1)
        ]
        if rows:
            await self._session.execute(test_results.insert(), rows)

    async def run_repo_id(self, *, org_id: str, run_id: str) -> str | None:
        """The repo a stored run belongs to, or None if it is not in this org."""
        try:
            canonical_org, canonical_run = (
                str(uuid.UUID(org_id)),
                str(uuid.UUID(run_id)),
            )
        except ValueError:
            return None
        row = await self._session.execute(
            select(runs.c.repo_id).where(
                runs.c.org_id == canonical_org, runs.c.id == canonical_run
            )
        )
        found = row.scalar_one_or_none()
        return None if found is None else str(found)

    async def run_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> RunReport | None:
        """The run row, its processed upload, and attempts in (case, attempt) order."""
        run = await self._session.execute(
            select(
                runs.c.id,
                runs.c.upload_id,
                runs.c.source,
                runs.c.commit_sha,
                runs.c.environment,
                runs.c.revision_status,
                runs.c.received_at,
                runs.c.tests,
                runs.c.attempts,
                runs.c.passed,
                runs.c.failed,
                runs.c.errored,
                runs.c.skipped,
            ).where(
                runs.c.org_id == org_id,
                runs.c.repo_id == repo_id,
                runs.c.id == run_id,
            )
        )
        row = run.first()
        if row is None:
            return None
        upload = await self._session.execute(
            select(
                uploads.c.ci_source,
                uploads.c.ci_run_id,
                uploads.c.ci_attempt,
                uploads.c.job_name,
                uploads.c.commit_sha.label("metadata_commit_sha"),
                uploads.c.ref,
                uploads.c.environment.label("metadata_environment"),
                uploads.c.started_at,
                uploads.c.finished_at,
                uploads.c.content_sha256,
                uploads.c.size_bytes,
                uploads.c.artifact_key,
            ).where(
                uploads.c.org_id == org_id,
                uploads.c.repo_id == repo_id,
                uploads.c.id == row.upload_id,
            )
        )
        upload_row = upload.first()
        metadata = None
        if upload_row is not None:
            metadata = UploadMetadata(
                ci_source=upload_row.ci_source,
                ci_run_id=upload_row.ci_run_id,
                ci_attempt=upload_row.ci_attempt,
                job_name=upload_row.job_name,
                commit_sha=upload_row.metadata_commit_sha,
                ref=upload_row.ref,
                environment=upload_row.metadata_environment,
                started_at=upload_row.started_at,
                finished_at=upload_row.finished_at,
            )
        found = await self._session.execute(
            select(
                test_identities.c.id,
                test_identities.c.classname,
                test_identities.c.name,
                test_results.c.outcome,
            )
            .join(
                test_identities,
                (test_identities.c.org_id == test_results.c.org_id)
                & (test_identities.c.repo_id == test_results.c.repo_id)
                & (test_identities.c.id == test_results.c.test_id),
            )
            .where(
                test_results.c.org_id == org_id,
                test_results.c.repo_id == repo_id,
                test_results.c.run_id == run_id,
            )
            .order_by(
                test_identities.c.classname,
                test_identities.c.name,
                test_results.c.attempt,
            )
        )
        cases: dict[tuple[str, str], tuple[str, list[str]]] = {}
        for case_row in found:
            key = (case_row.classname, case_row.name)
            _, chain = cases.setdefault(key, (case_row.id, []))
            chain.append(case_row.outcome)
        return RunReport(
            run_id=row.id,
            org_id=org_id,
            repo_id=repo_id,
            upload_id=row.upload_id,
            source=row.source,
            commit_sha=row.commit_sha,
            environment=row.environment,
            revision_status=row.revision_status,
            received_at=row.received_at,
            summary=RunSummary(
                tests=row.tests,
                attempts=row.attempts,
                passed=row.passed,
                failed=row.failed,
                errored=row.errored,
                skipped=row.skipped,
            ),
            metadata=metadata,
            content_sha256=upload_row.content_sha256 if upload_row else None,
            size_bytes=upload_row.size_bytes if upload_row else None,
            artifact_key=upload_row.artifact_key if upload_row else None,
            results=tuple(
                ReportedCase(
                    classname=name[0],
                    name=name[1],
                    attempts=tuple(chain),
                    test_id=identity,
                )
                for name, (identity, chain) in cases.items()
            ),
        )

    async def runs_index(
        self, *, org_id: str, repo_id: str, limit: int
    ) -> tuple[RunIndexEntry, ...]:
        """The pair's run rows, newest first (`received_at` DESC NULLS LAST,
        then run ID DESC for a deterministic total order), at most `limit`."""
        if limit < 1:
            return ()
        found = await self._session.execute(
            select(
                runs.c.id,
                runs.c.source,
                runs.c.commit_sha,
                runs.c.environment,
                runs.c.revision_status,
                runs.c.received_at,
                runs.c.tests,
                runs.c.attempts,
                runs.c.passed,
                runs.c.failed,
                runs.c.errored,
                runs.c.skipped,
            )
            .where(runs.c.org_id == org_id, runs.c.repo_id == repo_id)
            .order_by(runs.c.received_at.desc().nulls_last(), runs.c.id.desc())
            .limit(limit)
        )
        return tuple(
            RunIndexEntry(
                run_id=row.id,
                source=row.source,
                commit_sha=row.commit_sha,
                environment=row.environment,
                revision_status=row.revision_status,
                received_at=row.received_at,
                summary=RunSummary(
                    tests=row.tests,
                    attempts=row.attempts,
                    passed=row.passed,
                    failed=row.failed,
                    errored=row.errored,
                    skipped=row.skipped,
                ),
            )
            for row in found
        )

    async def failure_trend(
        self,
        *,
        org_id: str,
        repo_id: str,
        start: datetime,
        end: datetime,
        bucket: str,
        limit: int,
    ) -> tuple[TrendBucket, ...]:
        if bucket not in ("day", "week"):
            raise ValueError("the trend bucket is 'day' or 'week'")
        if limit < 1:
            return ()
        failing = test_results.c.outcome.in_((Outcome.FAILED, Outcome.ERRORED))
        # One row per (run, identity): flaky when the first failing attempt
        # precedes the last passing one, i.e. a failure later recovered.
        per_test = (
            select(
                runs.c.id.label("run_id"),
                runs.c.environment,
                func.date_trunc(bucket, func.timezone("UTC", _CI_TIME)).label(
                    "bucket_start"
                ),
                func.bool_or(failing).label("failed"),
                func.coalesce(
                    func.min(test_results.c.attempt).filter(failing)
                    < func.max(test_results.c.attempt).filter(
                        test_results.c.outcome == Outcome.PASSED
                    ),
                    False,
                ).label("flaky"),
            )
            .join(
                runs,
                (runs.c.org_id == test_results.c.org_id)
                & (runs.c.repo_id == test_results.c.repo_id)
                & (runs.c.id == test_results.c.run_id),
            )
            .where(
                test_results.c.org_id == org_id,
                test_results.c.repo_id == repo_id,
                runs.c.source == RunSource.CI_UPLOAD,
                _CI_TIME > start,
                _CI_TIME <= end,
            )
            .group_by(
                runs.c.id,
                runs.c.environment,
                runs.c.received_at,
                runs.c.ci_completed_at,
                test_results.c.test_id,
            )
            .subquery()
        )
        found = await self._session.execute(
            select(
                per_test.c.bucket_start,
                per_test.c.environment,
                func.count(func.distinct(per_test.c.run_id)).label("runs"),
                func.count().label("test_runs"),
                func.count().filter(per_test.c.failed).label("failed_test_runs"),
                func.count().filter(per_test.c.flaky).label("flaky_test_runs"),
            )
            .group_by(per_test.c.bucket_start, per_test.c.environment)
            .order_by(per_test.c.bucket_start, per_test.c.environment)
            .limit(limit)
        )
        return tuple(
            TrendBucket(
                bucket_start=row.bucket_start.replace(tzinfo=UTC),
                environment=row.environment,
                runs=row.runs,
                test_runs=row.test_runs,
                failed_test_runs=row.failed_test_runs,
                flaky_test_runs=row.flaky_test_runs,
            )
            for row in found
        )

    async def observations(
        self, *, org_id: str, repo_id: str, test_id: str
    ) -> list[Observation] | None:
        known = await self._session.execute(
            select(test_identities.c.id).where(
                test_identities.c.org_id == org_id,
                test_identities.c.repo_id == repo_id,
                test_identities.c.id == test_id,
            )
        )
        if known.first() is None:
            return None
        found = await self._session.execute(
            select(
                runs.c.id,
                runs.c.commit_sha,
                runs.c.environment,
                runs.c.received_at,
                runs.c.source,
                test_results.c.outcome,
            )
            .join(
                runs,
                (runs.c.org_id == test_results.c.org_id)
                & (runs.c.repo_id == test_results.c.repo_id)
                & (runs.c.id == test_results.c.run_id),
            )
            .where(
                test_results.c.org_id == org_id,
                test_results.c.repo_id == repo_id,
                test_results.c.test_id == test_id,
            )
            .order_by(runs.c.received_at, runs.c.id, test_results.c.attempt)
        )
        heads: dict[str, Any] = {}
        attempts: dict[str, list[Outcome]] = {}
        for row in found:
            heads.setdefault(row.id, row)
            attempts.setdefault(row.id, []).append(Outcome(row.outcome))
        return [
            Observation(
                run_id=run_id,
                commit_sha=row.commit_sha,
                environment=row.environment,
                received_at=row.received_at,
                source=RunSource(row.source),
                attempts=tuple(attempts[run_id]),
            )
            for run_id, row in heads.items()
        ]

    async def repo_history(
        self,
        *,
        org_id: str,
        repo_id: str,
        cutoff: datetime,
        max_tests: int = 100,
    ) -> tuple[IdentityHistory, ...]:
        if max_tests < 1:
            return ()
        # Identities with activity at or before the cutoff, newest activity first.
        active = await self._session.execute(
            select(
                test_identities.c.id,
                func.max(_CI_TIME).label("latest"),
            )
            .join(
                test_results,
                (test_results.c.org_id == test_identities.c.org_id)
                & (test_results.c.repo_id == test_identities.c.repo_id)
                & (test_results.c.test_id == test_identities.c.id),
            )
            .join(
                runs,
                (runs.c.org_id == test_results.c.org_id)
                & (runs.c.repo_id == test_results.c.repo_id)
                & (runs.c.id == test_results.c.run_id),
            )
            .where(
                test_identities.c.org_id == org_id,
                test_identities.c.repo_id == repo_id,
                _CI_TIME <= cutoff,
            )
            .group_by(test_identities.c.id)
            .order_by(func.max(_CI_TIME).desc(), test_identities.c.id)
            .limit(max_tests)
        )
        chosen = {row.id: row.latest for row in active}
        if not chosen:
            return ()
        names = await self._session.execute(
            select(
                test_identities.c.id,
                test_identities.c.classname,
                test_identities.c.name,
            ).where(test_identities.c.id.in_(chosen))
        )
        labels: dict[str, tuple[str, str]] = {
            row.id: (row.classname, row.name) for row in names
        }
        # The histories, run at or before the cutoff only (leakage guard).
        rows = await self._session.execute(
            select(
                test_results.c.test_id,
                runs.c.id,
                runs.c.commit_sha,
                runs.c.environment,
                _CI_TIME.label("received_at"),
                runs.c.source,
                test_results.c.outcome,
            )
            .join(
                runs,
                (runs.c.org_id == test_results.c.org_id)
                & (runs.c.repo_id == test_results.c.repo_id)
                & (runs.c.id == test_results.c.run_id),
            )
            .where(
                test_results.c.org_id == org_id,
                test_results.c.repo_id == repo_id,
                test_results.c.test_id.in_(chosen),
                _CI_TIME <= cutoff,
            )
            .order_by(
                _CI_TIME,
                runs.c.id,
                test_results.c.attempt,
            )
        )
        grouped: dict[str, dict[str, Any]] = {}
        attempts_by_run: dict[tuple[str, str], list[Any]] = {}
        for row in rows:
            grouped.setdefault(row.test_id, {})[row.id] = row
            attempts_by_run.setdefault((row.test_id, row.id), []).append(
                Outcome(row.outcome)
            )
        histories: list[IdentityHistory] = []
        for test_id in chosen:
            observations = tuple(
                Observation(
                    run_id=run_id,
                    commit_sha=head.commit_sha,
                    environment=head.environment,
                    received_at=head.received_at,
                    source=RunSource(head.source),
                    attempts=tuple(attempts_by_run[(test_id, run_id)]),
                )
                for run_id, head in grouped.get(test_id, {}).items()
            )
            classname, name = labels[test_id]
            histories.append(
                IdentityHistory(
                    test_id=test_id,
                    classname=classname,
                    name=name,
                    observations=observations,
                )
            )
        return tuple(histories)
