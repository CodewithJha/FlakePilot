"""Export finished run reports to a write-only document copy (goal 4).

PostgreSQL stays the system of record: the exporter reads TI's own classified
run reports (`RunViewer`, operator trust) and writes each one, as a document,
to a `ReportSink` (MongoDB Atlas in production). Nothing reads a document back.

What leaves the process: IDs, counts, enum values, the commit SHA, the
environment label, and each test's attempt outcomes and `ti-classification/1`
verdict with its evidence. Test class and method names, upload metadata,
artifact keys and raw reports never do.

The key is `(org_id, repo_id, run_id, report_revision)`, where the revision is
the SHA-256 of the canonical exported JSON, so re-exporting an unchanged
report writes nothing and a verdict that changed as history grew becomes a
new revision. Every outcome is one `ti.report.exported` event (`inserted` or
`unchanged`) or one `ti.report.export_failed` event with the sink's reason
code; the first sink failure stops the batch.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from flakepilot.integrations.application.ports import ReportSink, ReportSinkError
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject
from flakepilot.test_intelligence.application.run_view import (
    MAX_INDEX_LIMIT,
    ClassifiedRunReport,
    RunViewer,
)
from flakepilot.test_intelligence.application.store import UnitOfWork

SCHEMA = "flakepilot.run_report/1"


class ExportStatus(StrEnum):
    INSERTED = "inserted"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True)
class ExportedReport:
    run_id: str
    status: ExportStatus
    report_revision: str | None = None
    reason: str | None = None


def export_document(report: ClassifiedRunReport) -> dict[str, Any]:
    """The exported fields only, in a canonical (JSON-stable) shape."""
    run = report.run
    return {
        "schema": SCHEMA,
        "org_id": run.org_id,
        "repo_id": run.repo_id,
        "run_id": run.run_id,
        "source": run.source,
        "commit_sha": run.commit_sha,
        "environment": run.environment,
        "revision_status": run.revision_status,
        "summary": {
            "tests": run.summary.tests,
            "attempts": run.summary.attempts,
            "passed": run.summary.passed,
            "failed": run.summary.failed,
            "errored": run.summary.errored,
            "skipped": run.summary.skipped,
        },
        "results": [
            {
                "test_id": entry.case.test_id,
                "attempts": list(entry.case.attempts),
                "classification": None
                if entry.classification is None
                else {
                    "failure_class": entry.classification.failure_class.value,
                    "reason": entry.classification.reason.value
                    if entry.classification.reason
                    else None,
                    "target_sha": entry.classification.target_sha,
                    "rule_version": entry.classification.rule_version,
                    "evidence": dict(entry.classification.evidence),
                },
            }
            for entry in sorted(report.cases, key=lambda e: e.case.test_id)
        ],
    }


def report_revision(document: dict[str, Any]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def export_key(document: dict[str, Any], revision: str) -> str:
    return ":".join(
        (document["org_id"], document["repo_id"], document["run_id"], revision)
    )


class ReportExport:
    def __init__(
        self,
        unit_of_work: UnitOfWork,
        sink: ReportSink,
        runs: RunViewer | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._runs = runs or RunViewer(unit_of_work)
        self._sink = sink

    async def export(
        self, *, org_id: str, repo_id: str, limit: int, origin: Origin
    ) -> tuple[ExportedReport, ...]:
        """The pair's newest `limit` stored runs (at most `MAX_INDEX_LIMIT`),
        newest first. Raises `ResourceNotFound` for a malformed pair."""
        if not 1 <= limit <= MAX_INDEX_LIMIT:
            raise ValueError(f"limit is 1-{MAX_INDEX_LIMIT}")
        index = await self._runs.runs_index(org_id=org_id, repo_id=repo_id, limit=limit)
        outcomes: list[ExportedReport] = []
        for entry in index.entries:
            report = await self._runs.classified_report(
                org_id=org_id, repo_id=repo_id, run_id=entry.run_id
            )
            document = export_document(report)
            revision = report_revision(document)
            try:
                written = await self._sink.upsert(
                    export_key(document, revision),
                    {**document, "report_revision": revision},
                )
            except ReportSinkError as error:
                outcome = ExportedReport(
                    entry.run_id, ExportStatus.FAILED, revision, error.reason
                )
                await self._record(org_id, repo_id, outcome, origin)
                outcomes.append(outcome)
                break
            outcome = ExportedReport(
                entry.run_id,
                ExportStatus.INSERTED if written else ExportStatus.UNCHANGED,
                revision,
            )
            await self._record(org_id, repo_id, outcome, origin)
            outcomes.append(outcome)
        return tuple(outcomes)

    async def _record(
        self, org_id: str, repo_id: str, outcome: ExportedReport, origin: Origin
    ) -> None:
        failed = outcome.status is ExportStatus.FAILED
        async with self._unit_of_work() as store:
            await store.append_event(
                Event(
                    "ti.report.export_failed" if failed else "ti.report.exported",
                    Actor(ActorType.OPERATOR_CLI),
                    origin,
                    Subject("run", outcome.run_id),
                    org_id=org_id,
                    repo_id=repo_id,
                    after=outcome.status.value,
                    reason=outcome.reason,
                    payload={
                        "sink": "mongodb_atlas",
                        "report_revision": outcome.report_revision,
                    },
                )
            )
