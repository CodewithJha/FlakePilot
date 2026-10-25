"""A synthetic classified run report and an in-memory report sink (goal 4 tests)."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from flakepilot.integrations.application.ports import ReportSinkError
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedCase,
    ClassifiedRunReport,
)
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunReport,
    RunSummary,
)
from flakepilot.test_intelligence.domain.classification import (
    Classification,
    FailureClass,
)
from flakepilot.test_intelligence.domain.results import UploadMetadata

SECRET_NAME = "test_never_exported_name"


def synthetic_report(
    org_id: str | None = None, repo_id: str | None = None, run_id: str | None = None
) -> ClassifiedRunReport:
    org_id = org_id or str(uuid.uuid4())
    repo_id = repo_id or str(uuid.uuid4())
    run_id = run_id or str(uuid.uuid4())
    finished = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    flaky = ReportedCase(
        classname="pkg.SecretSuite",
        name=SECRET_NAME,
        attempts=("failed", "passed"),
        test_id="00000000-0000-4000-8000-000000000002",
    )
    passing = ReportedCase(
        classname="pkg.SecretSuite",
        name="test_also_private",
        attempts=("passed",),
        test_id="00000000-0000-4000-8000-000000000001",
    )
    return ClassifiedRunReport(
        run=RunReport(
            run_id=run_id,
            org_id=org_id,
            repo_id=repo_id,
            upload_id=str(uuid.uuid4()),
            source="ci_upload",
            commit_sha="ab" * 20,
            environment="ubuntu-24.04",
            revision_status="unchecked",
            received_at=finished,
            summary=RunSummary(2, 3, 2, 0, 0, 0),
            metadata=UploadMetadata(
                ci_source="github_actions",
                ci_run_id="private-run-id",
                ci_attempt=1,
                job_name="private job",
                commit_sha="ab" * 20,
                ref="refs/heads/private-branch",
                environment="ubuntu-24.04",
                started_at=finished,
                finished_at=finished,
            ),
            content_sha256=b"\x00" * 32,
            size_bytes=123,
            artifact_key="ti/private/artifact/key",
            results=(flaky, passing),
        ),
        cases=(
            ClassifiedCase(
                flaky,
                Classification(
                    FailureClass.FLAKY,
                    None,
                    "ab" * 20,
                    {"environment": "ubuntu-24.04", "runs": [run_id]},
                ),
            ),
            ClassifiedCase(passing, None),
        ),
    )


class MemorySink:
    """`$setOnInsert` semantics over a dict; `fail` makes every write refuse."""

    def __init__(self, fail: str | None = None) -> None:
        self.documents: dict[str, dict[str, object]] = {}
        self.fail = fail

    async def upsert(self, key: str, document: Mapping[str, object]) -> bool:
        if self.fail is not None:
            raise ReportSinkError(self.fail)
        if key in self.documents:
            return False
        self.documents[key] = dict(document)
        return True
