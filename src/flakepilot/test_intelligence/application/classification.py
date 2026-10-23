"""Classify one test identity on demand (ADR-013 deviation 5: not stored, no event).

Read-only and deterministic. Needs `repo.view`. The code-host standing comes from the
caller: REPO's `CodeHostAccessVerifier` result, resolved by the composition (same as
IAM's use cases). A test identity outside the org and repo is not found.
"""

import uuid

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    ResourceNotFound,
    authorize,
)
from flakepilot.test_intelligence.application.store import UnitOfWork
from flakepilot.test_intelligence.domain.classification import Classification, classify
from flakepilot.test_intelligence.domain.results import SHA


class TestClassification:
    __test__ = False  # not a pytest test class

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def classify(
        self,
        access: OrgAccess,
        *,
        org_id: str,
        repo_id: str,
        test_id: str,
        code_host: CodeHostAccess,
        revision: str | None = None,
    ) -> Classification:
        authorize(
            access, Grant.VIEW, org_id=org_id, repo_id=repo_id, code_host=code_host
        )
        if not _is_uuid(test_id) or (
            revision is not None and not SHA.fullmatch(revision)
        ):
            raise ResourceNotFound
        async with self._unit_of_work() as store:
            observations = await store.observations(
                org_id=org_id, repo_id=repo_id, test_id=test_id
            )
        if observations is None:
            raise ResourceNotFound
        return classify(observations, target_sha=revision)


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
