"""Read-only view of one stored CI run (operator-demo reporting).

This use case reports what a run's ingestion stored, without claiming anything
the stored data does not. The caller supplies the (org, repo) scope explicitly;
it is the operator CLI, the trust `bootstrap-org` already establishes. The
classification use case exists but is not part of this MVP (ADR-013 deviation
5): a stored attempt chain is shown, not a classifier verdict.
"""

import uuid

from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.test_intelligence.application.store import RunReport, UnitOfWork


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


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
