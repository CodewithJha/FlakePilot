"""Read-only view of one stored CI run.

This use case reports what a run's ingestion stored, without claiming anything
the stored data does not.

`classified_report` is the operator-CLI path: the caller supplies the (org,
repo) scope explicitly, the trust `bootstrap-org` already establishes.

`classified_report_for` is the authenticated-user path. The caller supplies an
`OrgAccess` already built by IAM from its own records for a verified session
principal (never from request input), and the code-host standing REPO's
`CodeHostAccessVerifier` verified for this caller and this run's repository.
It mirrors the shipped execution view
(`execution.application.reproduction.inspect`): a run outside the caller's org
is not found — a cross-org request learns nothing — and `repo.view` is decided
under PRODUCT-SCOPE §3.1's intersection rule with the verified standing. The
composition root resolves the standing and denies (fail closed, non-disclosing)
before this use case when verification could not be completed. The revision
status stays `unchecked` in any case: revision verification is a separate,
real mechanism (`pin_revision`), not access verification.

Both attach each test's verdict from the stored evidence by calling the only
classification implementation there is: the deterministic, versioned domain
rule set `ti-classification/1` (`domain.classification`). They report the rule
set's own target selection, reasons and evidence; they never add confidence or
labels the rules do not produce.
"""

import uuid
from dataclasses import dataclass

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    ResourceNotFound,
    authorize,
)
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
            return await self._load(
                store, org_id=org_id, repo_id=repo_id, run_id=run_id
            )

    async def classified_report_for(
        self,
        access: OrgAccess,
        *,
        org_id: str,
        repo_id: str,
        run_id: str,
        code_host: CodeHostAccess,
    ) -> ClassifiedRunReport:
        """The authenticated view of one run in the caller's org.

        `access` is IAM's record for the verified session principal; the org in
        the URL is only a lookup key. The caller has already resolved the run's
        repo inside that org (a run in another org is not found there — a
        cross-org request learns nothing) and the code-host standing REPO's
        `CodeHostAccessVerifier` verified for this caller and this repository;
        the composition resolves it and passes the verified result (unverified
        outcomes deny before this call). `repo.view` is then authorized under
        the same intersection rule as the operator path's sibling use cases.
        """
        if not (_is_uuid(org_id) and _is_uuid(repo_id) and _is_uuid(run_id)):
            raise ResourceNotFound
        async with self._unit_of_work() as store:
            authorize(
                access,
                Grant.VIEW,
                org_id=org_id,
                repo_id=repo_id,
                code_host=code_host,
            )
            return await self._load(
                store, org_id=org_id, repo_id=repo_id, run_id=run_id
            )

    async def _load(
        self, store: TiStore, *, org_id: str, repo_id: str, run_id: str
    ) -> ClassifiedRunReport:
        """The report plus per-test verdicts, inside the caller's unit of work."""
        report = await store.run_report(org_id=org_id, repo_id=repo_id, run_id=run_id)
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
        return ClassifiedRunReport(run=report, cases=tuple(cases))

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
