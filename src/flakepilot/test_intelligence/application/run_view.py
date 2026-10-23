"""Read-only view of one stored CI run (operator-demo reporting).

This use case reports what a run's ingestion stored, without claiming anything
the stored data does not. The caller supplies the (org, repo) scope explicitly;
it is the operator CLI, the trust `bootstrap-org` already establishes.

`classified_report` attaches each test's verdict from the stored evidence, by
calling the only classification implementation there is: the deterministic,
versioned domain rule set `ti-classification/1` (`domain.classification`). It
reports the rule set's own target selection, reasons and evidence; it never
adds confidence or labels the rules do not produce. The session-user use case
(`application.classification`) stays the authorization-checked path.
"""

import uuid
from dataclasses import dataclass

from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunReport,
    TiStore,
    UnitOfWork,
)
from flakepilot.test_intelligence.domain.classification import (
    Classification,
    classify,
)


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class ClassifiedCase:
    """One reported case with the verdict its stored evidence supports."""

    case: ReportedCase
    classification: Classification | None


@dataclass(frozen=True)
class ClassifiedRunReport:
    """A run's report with per-test classifications. Read-only throughout."""

    run: RunReport
    cases: tuple[ClassifiedCase, ...]


class RunViewer:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def report(self, *, org_id: str, repo_id: str, run_id: str) -> RunReport:
        """One run's report, read-only. An ID outside the pair is not found."""
        if not (_is_uuid(org_id) and _is_uuid(repo_id) and _is_uuid(run_id)):
            raise ResourceNotFound
        async with self._unit_of_work() as store:
            report = await store.run_report(
                org_id=org_id, repo_id=repo_id, run_id=run_id
            )
        if report is None:
            raise ResourceNotFound
        return report

    async def classified_report(
        self, *, org_id: str, repo_id: str, run_id: str
    ) -> ClassifiedRunReport:
        """The run's report plus each test's classification over its history.

        One unit of work: the report and the evidence are read consistently.
        The classification target follows the rule set's own selection rule
        (the asked-for revision, else the most recently first-seen SHA with a
        failing attempt), and is part of the rendered verdict.
        """
        if not (_is_uuid(org_id) and _is_uuid(repo_id) and _is_uuid(run_id)):
            raise ResourceNotFound
        async with self._unit_of_work() as store:
            report = await store.run_report(
                org_id=org_id, repo_id=repo_id, run_id=run_id
            )
            if report is None:
                raise ResourceNotFound
            cases: list[ClassifiedCase] = []
            for case in report.results:
                cases.append(
                    ClassifiedCase(
                        case=case,
                        classification=await self._classify(store, report, case),
                    )
                )
            cases_tuple = tuple(cases)
        return ClassifiedRunReport(run=report, cases=cases_tuple)

    async def _classify(
        self, store: TiStore, report: RunReport, case: ReportedCase
    ) -> Classification | None:
        """The rule set's verdict for this identity's stored CI evidence.

        An identity that is not in this (org, repo) pair has no verdict here:
        the renderers say so rather than guessing a class.
        """
        observations = await store.observations(
            org_id=report.org_id, repo_id=report.repo_id, test_id=case.test_id
        )
        if observations is None:
            return None
        return classify(observations)
