"""Backfill: original CI time with explicit provenance, idempotent, audited."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from actions_support import junit
from support import InMemoryArtifactStore

from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.shared_kernel.events import Channel, Event, Origin
from flakepilot.test_intelligence.application.backfill import (
    BACKFILL_SOURCE,
    Backfill,
    BackfillStatus,
    HistoricalReport,
)
from flakepilot.test_intelligence.application.store import (
    NewUpload,
    ProcessedUpload,
    RunSummary,
    TiStore,
    UnitOfWork,
)
from flakepilot.test_intelligence.domain.results import (
    TestIdentity,
    TestResult,
    UploadMetadata,
)

ORG, REPO = str(uuid.uuid4()), str(uuid.uuid4())
ORIGIN = Origin(Channel.CLI, "backfill-test")
COMPLETED = datetime(2026, 9, 3, 14, 5, tzinfo=UTC)
REPORT = HistoricalReport(
    ci_run_id="9001",
    ci_attempt=1,
    job_name="junit-linux",
    commit_sha="ab" * 20,
    ref="refs/heads/main",
    environment="github-actions/junit-linux",
    started_at=COMPLETED.replace(minute=0),
    completed_at=COMPLETED,
    run_url="https://github.com/acme/web/actions/runs/9001/attempts/1",
)
XML = junit(("tests.a", "test_one", "passed"), ("tests.a", "test_two", "flaky"))


class FakeStore:
    def __init__(self) -> None:
        self.uploads: dict[tuple[object, ...], tuple[NewUpload, str, RunSummary]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.results: list[tuple[str, str, TestResult]] = []
        self.events: list[Event] = []

    @staticmethod
    def _key(org_id: str, repo_id: str, m: UploadMetadata) -> tuple[object, ...]:
        return (org_id, repo_id, m.ci_source, m.ci_run_id, m.ci_attempt, m.job_name)

    async def append_event(self, event: Event) -> None:
        self.events.append(event)

    async def processed_upload(
        self, *, org_id: str, repo_id: str, metadata: UploadMetadata
    ) -> ProcessedUpload | None:
        found = self.uploads.get(self._key(org_id, repo_id, metadata))
        if found is None:
            return None
        upload, run_id, summary = found
        return ProcessedUpload(upload.upload_id, run_id, upload.content_sha256, summary)

    async def claim_upload(self, upload: NewUpload) -> bool:
        key = self._key(upload.org_id, upload.repo_id, upload.metadata)
        if key in self.uploads:
            return False
        self.uploads[key] = (upload, "", RunSummary(0, 0, 0, 0, 0, 0))
        return True

    async def ensure_identities(
        self, *, org_id: str, repo_id: str, identities: Sequence[TestIdentity]
    ) -> dict[TestIdentity, str]:
        return {i: f"{i.classname}.{i.name}" for i in identities}

    async def create_run(self, **row: Any) -> None:
        self.runs[row["run_id"]] = row
        for key, (upload, _, _) in self.uploads.items():
            if upload.upload_id == row["upload_id"]:
                self.uploads[key] = (upload, row["run_id"], row["summary"])

    async def add_results(
        self,
        *,
        org_id: str,
        repo_id: str,
        run_id: str,
        results: Sequence[tuple[str, TestResult]],
    ) -> None:
        self.results.extend((run_id, test_id, r) for test_id, r in results)


def _backfill(store: FakeStore) -> tuple[Backfill, InMemoryArtifactStore]:
    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[TiStore]:
        yield cast(TiStore, store)

    artifacts = InMemoryArtifactStore()
    uow: UnitOfWork = unit_of_work
    return Backfill(uow, artifacts, JUnitParser()), artifacts


def _import(
    backfill: Backfill,
    report: HistoricalReport = REPORT,
    archive: bytes = b"zip-1",
    documents: Sequence[bytes] = (XML,),
) -> Any:
    return asyncio.run(
        backfill.import_report(
            org_id=ORG,
            repo_id=REPO,
            report=report,
            archive=archive,
            documents=documents,
            origin=ORIGIN,
        )
    )


def test_an_import_keeps_original_time_and_names_its_provenance() -> None:
    store = FakeStore()
    backfill, artifacts = _backfill(store)
    result = _import(backfill)
    assert result.status is BackfillStatus.IMPORTED and result.tests == 2
    ((upload, run_id, summary),) = store.uploads.values()
    assert run_id == result.run_id and summary.passed == 2
    assert upload.token_id is None
    assert upload.provenance == BACKFILL_SOURCE
    assert upload.origin_run_url == REPORT.run_url
    assert upload.metadata.ci_source == BACKFILL_SOURCE
    assert upload.metadata.ci_run_id == "9001"
    assert upload.metadata.finished_at == COMPLETED
    assert upload.content_type == "application/zip"
    assert list(artifacts.objects.values()) == [b"zip-1"]
    assert store.runs[run_id]["ci_completed_at"] == COMPLETED
    (event,) = store.events
    assert event.name == "ti.backfill.imported"
    assert event.actor.type.value == "operator_cli" and event.actor.id is None
    assert event.payload["ci_completed_at"] == COMPLETED.isoformat()
    assert event.payload["source"] == BACKFILL_SOURCE
    flaky = next(r for _, t, r in store.results if t.endswith("test_two"))
    assert [a.value for a in flaky.attempts] == ["failed", "passed"]


def test_reimporting_the_same_artifact_writes_nothing_new() -> None:
    store = FakeStore()
    backfill, artifacts = _backfill(store)
    first = _import(backfill)
    again = _import(backfill)
    assert again.status is BackfillStatus.DUPLICATE
    assert (again.upload_id, again.run_id) == (first.upload_id, first.run_id)
    assert len(store.uploads) == 1 and len(store.runs) == 1 and len(store.results) == 2
    assert len(artifacts.objects) == 1
    assert [e.name for e in store.events] == [
        "ti.backfill.imported",
        "ti.backfill.duplicate",
    ]


def test_the_same_key_with_another_archive_is_refused() -> None:
    store = FakeStore()
    backfill, _ = _backfill(store)
    _import(backfill)
    conflict = _import(backfill, archive=b"zip-2")
    assert conflict.status is BackfillStatus.REJECTED
    assert conflict.reason == "idempotency_conflict"
    assert len(store.runs) == 1
    assert store.events[-1].name == "ti.backfill.rejected"
    assert store.events[-1].reason == "idempotency_conflict"


def test_another_attempt_of_the_same_run_is_a_new_run() -> None:
    store = FakeStore()
    backfill, _ = _backfill(store)
    _import(backfill)
    retry = _import(backfill, replace(REPORT, ci_attempt=2), archive=b"zip-2")
    assert retry.status is BackfillStatus.IMPORTED
    assert len(store.runs) == 2


def test_non_junit_xml_is_ignored_and_bad_reports_are_audited_refusals() -> None:
    store = FakeStore()
    backfill, _ = _backfill(store)
    mixed = _import(backfill, documents=(b"<coverage/>", XML))
    assert mixed.status is BackfillStatus.IMPORTED and mixed.tests == 2
    cases = [
        ((b"<coverage/>",), REPORT, "no_test_cases"),
        ((b"<testsuite><testcase",), REPORT, "malformed_xml"),
        ((XML,), replace(REPORT, ref=""), "invalid_metadata"),
        ((XML,), replace(REPORT, job_name=" spaced "), "invalid_metadata"),
        ((XML,), replace(REPORT, run_url="http://github.com/x"), "invalid_metadata"),
        (
            (XML,),
            replace(REPORT, started_at=COMPLETED.replace(hour=20)),
            "invalid_metadata",
        ),
    ]
    for number, (documents, report, reason) in enumerate(cases, start=2):
        result = _import(
            backfill, replace(report, ci_run_id=str(number)), documents=documents
        )
        assert (result.status, result.reason) == (BackfillStatus.REJECTED, reason)
    assert len(store.runs) == 1
    refusals = [e for e in store.events if e.name == "ti.backfill.rejected"]
    assert [e.reason for e in refusals] == [reason for _, _, reason in cases]
    assert all(e.org_id == ORG and e.repo_id == REPO for e in store.events)
