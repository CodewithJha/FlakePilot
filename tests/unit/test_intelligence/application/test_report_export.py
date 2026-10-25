"""Report export (goal 4): exported fields only, idempotent key, audited, fail-stop."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import pytest
from atlas_support import SECRET_NAME, MemorySink, synthetic_report

from flakepilot.shared_kernel.events import Channel, Event, Origin
from flakepilot.test_intelligence.application.report_export import (
    ExportStatus,
    ReportExport,
    export_document,
    export_key,
    report_revision,
)
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedRunReport,
    RunIndexReport,
    RunViewer,
)
from flakepilot.test_intelligence.application.store import (
    RunIndexEntry,
    TiStore,
    UnitOfWork,
)

ORG, REPO = str(uuid.uuid4()), str(uuid.uuid4())
ORIGIN = Origin(Channel.CLI, "export-test")


class Events:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def append_event(self, event: Event) -> None:
        self.events.append(event)


class Viewer:
    def __init__(self, reports: list[ClassifiedRunReport]) -> None:
        self.reports = {r.run.run_id: r for r in reports}

    async def runs_index(
        self, *, org_id: str, repo_id: str, limit: int
    ) -> RunIndexReport:
        entries = tuple(
            RunIndexEntry(
                run_id=r.run.run_id,
                source=r.run.source,
                commit_sha=r.run.commit_sha,
                environment=r.run.environment,
                revision_status=r.run.revision_status,
                received_at=r.run.received_at,
                summary=r.run.summary,
            )
            for r in self.reports.values()
        )[:limit]
        return RunIndexReport(org_id, repo_id, entries)

    async def classified_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> ClassifiedRunReport:
        return self.reports[run_id]


def _export(
    sink: MemorySink, reports: list[ClassifiedRunReport], limit: int = 50
) -> tuple[Any, list[Event]]:
    events = Events()

    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[TiStore]:
        yield cast(TiStore, events)

    uow: UnitOfWork = unit_of_work
    export = ReportExport(uow, sink, cast(RunViewer, Viewer(reports)))
    outcomes = asyncio.run(
        export.export(org_id=ORG, repo_id=REPO, limit=limit, origin=ORIGIN)
    )
    return outcomes, events.events


def test_the_document_carries_ids_counts_and_verdicts_never_names() -> None:
    report = synthetic_report(ORG, REPO)
    document = export_document(report)
    rendered = json.dumps(document)
    for private in (
        SECRET_NAME,
        "SecretSuite",
        "private-run-id",
        "private job",
        "private-branch",
        "artifact/key",
    ):
        assert private not in rendered
    assert document["run_id"] == report.run.run_id
    assert document["summary"]["tests"] == 2
    assert [r["test_id"] for r in document["results"]] == sorted(
        c.case.test_id for c in report.cases
    )
    flaky = document["results"][1]
    assert flaky["attempts"] == ["failed", "passed"]
    assert flaky["classification"]["failure_class"] == "flaky"
    assert flaky["classification"]["rule_version"] == "ti-classification/1"


def test_the_revision_is_stable_and_follows_the_content() -> None:
    report = synthetic_report(ORG, REPO)
    first = report_revision(export_document(report))
    assert first == report_revision(export_document(report))
    other_upload = synthetic_report(ORG, REPO, run_id=report.run.run_id)
    assert report_revision(export_document(other_upload)) == first  # not exported
    verdict = replace(report.cases[0], classification=None)
    changed = replace(report, cases=(verdict, report.cases[1]))
    assert report_revision(export_document(changed)) != first
    key = export_key(export_document(report), first)
    assert key == f"{ORG}:{REPO}:{report.run.run_id}:{first}"


def test_a_re_export_is_unchanged_and_both_are_audited() -> None:
    sink = MemorySink()
    reports = [synthetic_report(ORG, REPO), synthetic_report(ORG, REPO)]
    first, events = _export(sink, reports)
    assert [o.status for o in first] == [ExportStatus.INSERTED] * 2
    again, more = _export(sink, reports)
    assert [o.status for o in again] == [ExportStatus.UNCHANGED] * 2
    assert len(sink.documents) == 2
    stored = next(iter(sink.documents.values()))
    assert stored["report_revision"] == first[0].report_revision
    assert [(e.name, e.after) for e in events + more] == [
        ("ti.report.exported", "inserted")
    ] * 2 + [("ti.report.exported", "unchanged")] * 2
    assert all(e.payload["sink"] == "mongodb_atlas" for e in events)


def test_the_first_sink_failure_stops_the_batch_with_its_reason() -> None:
    reports = [synthetic_report(ORG, REPO), synthetic_report(ORG, REPO)]
    outcomes, events = _export(MemorySink(fail="atlas_unreachable"), reports)
    assert [(o.status, o.reason) for o in outcomes] == [
        (ExportStatus.FAILED, "atlas_unreachable")
    ]
    assert [(e.name, e.reason) for e in events] == [
        ("ti.report.export_failed", "atlas_unreachable")
    ]


def test_the_limit_is_bounded() -> None:
    with pytest.raises(ValueError):
        _export(MemorySink(), [], limit=201)
    outcomes, _ = _export(MemorySink(), [synthetic_report(ORG, REPO)] * 1, limit=1)
    assert len(outcomes) == 1
