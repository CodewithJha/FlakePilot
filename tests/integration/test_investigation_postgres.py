"""The investigation workflow over real `ti` and `diag` tables.

Seeds a run through `SqlTiStore`'s own write path (uploads FK included),
sets a diagnosis policy through the real `Assist.set_policy`, and runs
`Investigation.investigate` end to end with a real `RunViewer` and
`Reliability`. Asserts the stage choreography, the ranking from real stored
history, and the durable provenance rows in `diag.outbox`.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.investigation import (
    Investigation,
    StageName,
    StageStatus,
    StoppingCondition,
)
from flakepilot.diagnosis.application.store import AiPolicy, DiagStore
from flakepilot.diagnosis.domain.output import DataPolicy
from flakepilot.diagnosis.infrastructure.store import SqlDiagStore
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.integrations.infrastructure.ai import NullAiAssist
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.reliability import Reliability
from flakepilot.test_intelligence.application.run_view import RunViewer
from flakepilot.test_intelligence.application.store import NewUpload, RunSummary
from flakepilot.test_intelligence.domain.results import (
    Outcome,
    RunSource,
    TestIdentity,
    TestResult,
    UploadMetadata,
)
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration

FLAKY = TestIdentity("tests.payment", "test_cart_totals")
FAILING = TestIdentity("tests.payment", "test_refund")
NOW = datetime.now(UTC)
ORIGIN = Origin(Channel.CLI, "investigate-integration")


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def ti_uow(
    engine: AsyncEngine,
) -> Callable[[], AbstractAsyncContextManager[SqlTiStore]]:
    @asynccontextmanager
    async def unit() -> AsyncIterator[SqlTiStore]:
        async with transaction(engine) as session:
            yield SqlTiStore(session)

    return unit


def diag_uow(
    engine: AsyncEngine,
) -> Callable[[], AbstractAsyncContextManager[DiagStore]]:
    @asynccontextmanager
    async def unit() -> AsyncIterator[DiagStore]:
        async with transaction(engine) as session:
            yield SqlDiagStore(session)

    return unit


async def sql(engine: AsyncEngine, statement: str, **params: object) -> list[Any]:
    async with transaction(engine) as session:
        result = await session.execute(text(statement), params)
        if isinstance(result, CursorResult) and result.returns_rows:
            return list(result)
        return []


async def seed_run(
    engine: AsyncEngine, *, org_id: str, repo_id: str, primary_run_id: str | None = None
) -> tuple[str, str]:
    """Two runs through TI's own write path: a flaky identity and an always-
    failing one. Returns (primary run id, flaky identity ID)."""
    primary_run_id = primary_run_id or str(uuid.uuid4())
    async with transaction(engine) as session:
        s = SqlTiStore(session)
        ids = await s.ensure_identities(
            org_id=org_id, repo_id=repo_id, identities=[FLAKY, FAILING]
        )
        planted = (
            (FLAKY, (Outcome.FAILED, Outcome.PASSED)),
            (FAILING, (Outcome.FAILED,)),
        )
        for number, (identity, attempts) in enumerate(planted, start=1):
            upload_id = str(uuid.uuid4())
            claimed = await s.claim_upload(
                NewUpload(
                    upload_id=upload_id,
                    org_id=org_id,
                    repo_id=repo_id,
                    token_id=str(uuid.uuid4()),
                    metadata=UploadMetadata(
                        ci_source="github",
                        ci_run_id=f"run-{number}",
                        ci_attempt=1,
                        job_name="test",
                        commit_sha="a" * 40,
                        ref="refs/heads/main",
                        environment="ci-linux",
                        started_at=NOW,
                        finished_at=NOW,
                    ),
                    content_sha256=bytes(32),
                    size_bytes=1,
                    content_type="application/xml",
                    artifact_key=f"{org_id}/{repo_id}/{upload_id}.xml",
                )
            )
            assert claimed  # a fresh (org, repo, idempotency) triple always claims
            run_id = primary_run_id if number == 1 else str(uuid.uuid4())
            await s.create_run(
                run_id=run_id,
                upload_id=upload_id,
                org_id=org_id,
                repo_id=repo_id,
                source=RunSource.CI_UPLOAD,
                commit_sha="a" * 40,
                environment="ci-linux",
                summary=RunSummary(
                    tests=1,
                    attempts=len(attempts),
                    passed=sum(1 for a in attempts if a is Outcome.PASSED),
                    failed=sum(1 for a in attempts if a is Outcome.FAILED),
                    errored=0,
                    skipped=0,
                ),
            )
            await s.add_results(
                org_id=org_id,
                repo_id=repo_id,
                run_id=run_id,
                results=[
                    (ids[identity], TestResult(identity=identity, attempts=attempts))
                ],
            )
    return primary_run_id, ids[FLAKY]


def investigation(engine: AsyncEngine) -> tuple[Investigation, Assist]:
    """The real workflow over real stores; no research provider wired."""
    assist = Assist(diag_uow(engine), NullAiAssist())
    workflow = Investigation(
        runs=RunViewer(ti_uow(engine)),
        assist=assist,
        reliability=Reliability(ti_uow(engine)),
        unit_of_work=diag_uow(engine),
    )
    return workflow, assist


def _access(org_id: str, actor: str) -> OrgAccess:
    return OrgAccess(actor, org_id, org_admin=True)


def _actor() -> str:
    return str(uuid.uuid4())


def test_investigation_over_real_tables_records_provenance(url: str) -> None:
    org, repo, actor = str(uuid.uuid4()), str(uuid.uuid4()), _actor()

    async def scenario(engine: AsyncEngine) -> Any:
        run_id, flaky_id = await seed_run(engine, org_id=org, repo_id=repo)
        workflow, assist = investigation(engine)
        await assist.set_policy(
            _access(org, actor),
            org_id=org,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=ORIGIN,
        )
        report = await workflow.investigate(
            _access(org, actor),
            org_id=org,
            repo_id=repo,
            run_id=run_id,
            code_host=CodeHostAccess.READ,
            cutoff=datetime.now(UTC),
            origin=ORIGIN,
        )
        events = await sql(
            engine,
            "SELECT name, after, org_id, published_at IS NULL AS pending "
            "FROM diag.outbox WHERE org_id = :org ORDER BY seq",
            org=org,
        )
        return report, flaky_id, events

    report, flaky_id, events = run(url, scenario)

    # The workflow completed and every stage is an honest record.
    assert report.stopping is StoppingCondition.COMPLETE
    assert [stage.name for stage in report.stages] == [
        StageName.COLLECT,
        StageName.RESEARCH,
        StageName.HYPOTHESIZE,
        StageName.PRIORITIZE,
    ]
    statuses = {stage.name: stage for stage in report.stages}
    assert statuses[StageName.COLLECT].status is StageStatus.PRODUCED
    assert statuses[StageName.RESEARCH].status is StageStatus.SKIPPED
    assert statuses[StageName.RESEARCH].reason == "provider_absent"
    # Policy is on, but no model is wired: skip with the port's short code.
    assert statuses[StageName.HYPOTHESIZE].status is StageStatus.SKIPPED
    assert statuses[StageName.HYPOTHESIZE].reason == "no_provider"
    # Real stored history: the flaky identity is ranked first.
    assert statuses[StageName.PRIORITIZE].status is StageStatus.PRODUCED
    assert report.prioritization is not None
    assert report.prioritization.ranked[0].test_id == flaky_id
    assert report.prioritization.ranked[0].rank == 1
    assert report.reliability is not None
    assert any(h.test_id == flaky_id for h in report.reliability.histories)

    # Durable provenance: the assist's event and exactly one investigation
    # event, both pending publication, scoped to the org.
    names = [row.name for row in events]
    assert names.count("diag.investigation.recorded") == 1
    investigation_rows = [
        row for row in events if row.name == "diag.investigation.recorded"
    ]
    assert investigation_rows[0].after == "complete"
    assert str(investigation_rows[0].org_id) == org
    assert investigation_rows[0].pending is True
    assert "diag.diagnosis.recorded" in names  # the assist stage's own record


def test_a_missing_run_records_not_found_and_raises(url: str) -> None:
    org, repo, actor = str(uuid.uuid4()), str(uuid.uuid4()), _actor()

    async def scenario(engine: AsyncEngine) -> Any:
        workflow, _ = investigation(engine)
        try:
            await workflow.investigate(
                _access(org, actor),
                org_id=org,
                repo_id=repo,
                run_id=str(uuid.uuid4()),
                code_host=CodeHostAccess.READ,
                cutoff=datetime.now(UTC),
                origin=ORIGIN,
            )
        except ResourceNotFound:
            pass
        else:
            raise AssertionError("expected ResourceNotFound")
        return await sql(
            engine,
            "SELECT name, after FROM diag.outbox WHERE org_id = :org",
            org=org,
        )

    events = run(url, scenario)
    investigation_rows = [
        row for row in events if row.name == "diag.investigation.recorded"
    ]
    assert [row.after for row in investigation_rows] == ["not_found"]
    assert "diag.diagnosis.recorded" not in [row.name for row in events]


def test_an_outside_principal_gets_not_found_recorded_and_raised(url: str) -> None:
    """A principal who cannot view the repo learns nothing: NOT_FOUND, and the
    recorded stopping condition is the same."""
    org, repo, actor = str(uuid.uuid4()), str(uuid.uuid4()), _actor()

    async def scenario(engine: AsyncEngine) -> Any:
        workflow, _ = investigation(engine)
        stranger = OrgAccess(actor, org, org_admin=False, grants=frozenset())
        try:
            await workflow.investigate(
                stranger,
                org_id=org,
                repo_id=repo,
                run_id=str(uuid.uuid4()),
                code_host=CodeHostAccess.READ,
                cutoff=datetime.now(UTC),
                origin=ORIGIN,
            )
        except ResourceNotFound:
            pass
        else:
            raise AssertionError("expected ResourceNotFound")
        return await sql(
            engine,
            "SELECT name, after FROM diag.outbox WHERE org_id = :org",
            org=org,
        )

    events = run(url, scenario)
    investigation_rows = [
        row for row in events if row.name == "diag.investigation.recorded"
    ]
    assert [row.after for row in investigation_rows] == ["not_found"]


def test_a_member_without_the_grant_records_unauthorized_and_raises(url: str) -> None:
    """A member who can view but lacks `repo.investigate` is FORBIDDEN: the
    workflow records the refusal before propagating it."""
    org, repo, actor = str(uuid.uuid4()), str(uuid.uuid4()), _actor()

    async def scenario(engine: AsyncEngine) -> Any:
        workflow, _ = investigation(engine)
        viewer = OrgAccess(
            actor, org, org_admin=False, grants=frozenset({(repo, Grant.VIEW)})
        )
        try:
            await workflow.investigate(
                viewer,
                org_id=org,
                repo_id=repo,
                run_id=str(uuid.uuid4()),
                code_host=CodeHostAccess.READ,
                cutoff=datetime.now(UTC),
                origin=ORIGIN,
            )
        except PermissionDenied:
            pass
        else:
            raise AssertionError("expected PermissionDenied")
        return await sql(
            engine,
            "SELECT name, after FROM diag.outbox WHERE org_id = :org",
            org=org,
        )

    events = run(url, scenario)
    investigation_rows = [
        row for row in events if row.name == "diag.investigation.recorded"
    ]
    assert [row.after for row in investigation_rows] == ["unauthorized"]


def test_an_empty_history_prioritizes_honestly(url: str) -> None:
    org, repo, actor = str(uuid.uuid4()), str(uuid.uuid4()), _actor()

    async def scenario(engine: AsyncEngine) -> Any:
        run_id, _ = await seed_run(engine, org_id=org, repo_id=repo)
        workflow, _ = investigation(engine)
        return await workflow.investigate(
            _access(org, actor),
            org_id=org,
            repo_id=repo,
            run_id=run_id,
            code_host=CodeHostAccess.READ,
            cutoff=NOW - timedelta(days=365 * 30),
            origin=ORIGIN,
        )

    report = run(url, scenario)
    prioritize_stage = next(
        stage for stage in report.stages if stage.name is StageName.PRIORITIZE
    )
    assert prioritize_stage.status is StageStatus.SKIPPED
    assert prioritize_stage.reason == "no_history_at_cutoff"
    assert report.prioritization is not None and report.prioritization.ranked == ()
    # The note states silence, never a fabricated ordering.
    assert "nothing to prioritize" in report.prioritization.note


@pytest.fixture
def url(migrated_database_url: str) -> str:
    return migrated_database_url
