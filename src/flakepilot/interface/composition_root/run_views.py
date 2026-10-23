"""The authenticated run view: one use case behind the session principal.

`RunViews` is what `interface.http.runs` calls. The `OrgAccess` arrives from
the `org_access` dependency (IAM's records for a verified session cookie); it
is never built from request input here. This is a read-only view of
already-stored uploads: it never fetches source, starts a runtime, or writes,
and closed-mode execution safety is untouched.
"""

from flakepilot.identity.application.authorization import OrgAccess
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedRunReport,
    RunViewer,
)
from flakepilot.test_intelligence.application.store import UnitOfWork

__all__ = ["ClassifiedRunReport", "RunViews"]


class RunViews:
    """Server-rendered run views for signed-in users."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def classified(
        self, *, access: OrgAccess, org_id: str, run_id: str
    ) -> ClassifiedRunReport:
        """The run report plus per-test verdicts, authorization-checked."""
        return await RunViewer(self._unit_of_work).classified_report_for(
            access=access, org_id=org_id, run_id=run_id
        )
