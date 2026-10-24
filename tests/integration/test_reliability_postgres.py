"""Reliability analytics and prioritization over real `ti` tables.

Seeds runs and results through `SqlTiStore`'s own write path (no SQL
shortcuts) and exercises `repo_history`, `Reliability`, and `prioritize`
against real rows: the cutoff excludes genuinely older runs, scope is
(org, repo)-exact, the store bound holds, and the flakiest stored test comes
out ranked first.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.test_intelligence.application.prioritization import prioritize
from flakepilot.test_intelligence.application.reliability import Reliability
from flakepilot.test_intelligence.application.store import (
    NewUpload,
    RunSummary,
)
from flakepilot.test_intelligence.domain.results import (
    Outcome,
    RunSource,
    TestIdentity,
    TestResult,
    UploadMetadata,
)
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)

FLAKY = TestIdentity("tests.payment", "test_cart_totals")
FAILING = TestIdentity("tests.payment", "test_refund")
GREEN = TestIdentity("tests.billing", "test_invoice")


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def unit_of_work(engine: AsyncEngine) -> Callable[[], Any]:
    @asynccontextmanager
    async def uow() -> AsyncIterator[SqlTiStore]:
        async with transaction(engine) as session:
            yield SqlTiStore(session)

    return uow


async def seed_planted(
    engine: AsyncEngine, *, org_id: str, repo_id: str
) -> dict[str, str]:
    """Four runs through the store's own write path. Run 1 and run 4 contain a
    flaky identity (FAILED then PASSED inside one run); a second always fails;
    a third stays green."""
    async with transaction(engine) as session:
        s = SqlTiStore(session)
        ids = await s.ensure_identities(
            org_id=org_id, repo_id=repo_id, identities=[FLAKY, FAILING, GREEN]
        )
        planted = (
            (FLAKY, (Outcome.FAILED, Outcome.PASSED)),
            (FAILING, (Outcome.FAILED,)),
            (GREEN, (Outcome.PASSED,)),
            (FLAKY, (Outcome.FAILED, Outcome.PASSED)),
        )
        for number, (identity, attempts) in enumerate(planted, start=1):
            run_id = str(uuid.uuid4())
            upload_id = str(uuid.uuid4())
            metadata = UploadMetadata(
                ci_source="github",
                ci_run_id=f"run-{number}",
                ci_attempt=1,
                job_name="test",
                commit_sha="a" * 40,
                ref="refs/heads/main",
                environment="ci-linux",
                started_at=NOW,
                finished_at=NOW,
            )
            await s.claim_upload(
                NewUpload(
                    upload_id=upload_id,
                    org_id=org_id,
                    repo_id=repo_id,
                    token_id=str(uuid.uuid4()),
                    metadata=metadata,
                    content_sha256=bytes(32),
                    size_bytes=1,
                    content_type="application/xml",
                    artifact_key=f"{org_id}/{repo_id}/{upload_id}.xml",
                )
            )
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
    return {
        "flaky": ids[FLAKY],
        "failing": ids[FAILING],
        "green": ids[GREEN],
    }


def test_repo_history_scopes_filters_and_bounds(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        org, repo = str(uuid.uuid4()), str(uuid.uuid4())
        ids = await seed_planted(engine, org_id=org, repo_id=repo)
        async with transaction(engine) as session:
            s = SqlTiStore(session)
            now = datetime.now(UTC)
            histories = await s.repo_history(org_id=org, repo_id=repo, cutoff=now)
            bounded = await s.repo_history(
                org_id=org, repo_id=repo, cutoff=now, max_tests=1
            )
            other_repo = await s.repo_history(
                org_id=org, repo_id=str(uuid.uuid4()), cutoff=now
            )
            ancient = await s.repo_history(
                org_id=org, repo_id=repo, cutoff=now - timedelta(days=3650)
            )
            beyond = await s.repo_history(
                org_id=org, repo_id=repo, cutoff=now, max_tests=0
            )
        return ids, histories, bounded, (other_repo, ancient, beyond)

    ids, histories, bounded, negatives = run(url, scenario)
    by_id = {h.test_id: h for h in histories}
    assert sorted(h.name for h in histories) == [
        "test_cart_totals",
        "test_invoice",
        "test_refund",
    ]
    flaky = by_id[ids["flaky"]]
    assert len(flaky.observations) == 2  # runs 1 and 4 contain the identity
    assert tuple(flaky.observations[0].attempts) == (Outcome.FAILED, Outcome.PASSED)
    assert len(bounded) == 1
    assert negatives == ((), (), ())  # another repo, an old cutoff, a zero bound


def test_reliability_and_prioritization_rank_real_history(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        org, repo = str(uuid.uuid4()), str(uuid.uuid4())
        ids = await seed_planted(engine, org_id=org, repo_id=repo)
        now = datetime.now(UTC)
        report = await Reliability(unit_of_work(engine)).report(
            org_id=org, repo_id=repo, cutoff=now
        )
        async with transaction(engine) as session:
            s = SqlTiStore(session)
            histories = await s.repo_history(org_id=org, repo_id=repo, cutoff=now)
        ancient = await Reliability(unit_of_work(engine)).report(
            org_id=org, repo_id=repo, cutoff=now - timedelta(days=3650)
        )
        return ids, (report, histories, ancient)

    ids, (report, histories, ancient) = run(url, scenario)
    # The flakiest stored test leads the report and the ranking alike.
    assert report.histories[0].test_id == ids["flaky"]
    assert report.histories[0].flaky_runs == 2 and report.histories[0].runs == 2
    failing = next(h for h in report.histories if h.test_id == ids["failing"])
    assert failing.flaky_runs == 0 and failing.failed_attempts == 1
    green = next(h for h in report.histories if h.test_id == ids["green"])
    assert green.flaky_runs == 0 and green.failed_attempts == 0

    ranking = prioritize(histories)
    assert ranking.ranked[0].test_id == ids["flaky"]
    assert ranking.ranked[0].rank == 1
    assert "Rank, not probability" in ranking.note
    assert ancient.histories == ()  # the cutoff excluded everything, honestly
    assert ids["green"]


@pytest.fixture
def url(migrated_database_url: str) -> str:
    return migrated_database_url
