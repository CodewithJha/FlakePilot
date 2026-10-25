"""The GitHub Actions backfill over real `ti` tables (scripted GitHub API).

Six historical runs, one day apart, are imported through the operator path
(`backfill_ci_history`): their original completion time lands on `ti.runs`
while receipt time stays now, the provenance is explicit, a re-import is a
no-op, the binding is tenant-exact, and the analytics (history cutoff, day
trend, the evaluation's temporal split) read CI time.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from actions_support import ActionsApi, FakeRun, FakeTokens, junit, zip_of
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from support import InMemoryArtifactStore

from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.integrations.infrastructure.github_actions import GitHubActionsHistory
from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.interface.composition_root import (
    Secret,
    Settings,
    analytics,
    ti_unit_of_work,
)
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.operator import backfill_ci_history
from flakepilot.repository.application.source import new_binding
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.test_intelligence.application.backfill import Backfill
from flakepilot.test_intelligence.application.evaluation import Evaluation, Status
from flakepilot.test_intelligence.application.store import NewUpload
from flakepilot.test_intelligence.domain.results import UploadMetadata
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

pytestmark = pytest.mark.integration

DAY0 = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
# Per day: the flaky test fails on even days, the broken one on days 3 and 5.
OUTCOMES = [
    (("flaky" if day % 2 == 0 else "passed"), ("failed" if day in (3, 5) else "passed"))
    for day in range(6)
]


@pytest.fixture
def url(migrated_database_url: str) -> str:
    return migrated_database_url


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _api(owner: str) -> ActionsApi:
    runs = [
        FakeRun(
            1000 + day,
            DAY0 + timedelta(days=day),
            {
                "junit-linux": zip_of(
                    {
                        "reports/TEST-a.xml": junit(
                            ("tests.a", "test_flaky", flaky),
                            ("tests.a", "test_broken", broken),
                            ("tests.a", "test_stable", "passed"),
                        ),
                        "coverage.xml": b"<coverage/>",
                    }
                ),
                "build-log": zip_of({"log.txt": b"not a report"}),
            },
        )
        for day, (flaky, broken) in enumerate(OUTCOMES)
    ]
    return ActionsApi(owner, "web", runs)


async def _bind(engine: AsyncEngine, owner: str) -> tuple[str, str]:
    org_id = str(uuid.uuid4())
    binding = new_binding(org_id=org_id, owner=owner, name="web")
    async with transaction(engine) as session:
        await SqlRepoDirectory(session).add(binding, now=datetime.now(UTC))
    return org_id, binding.repo_id


async def _backfill(
    engine: AsyncEngine, api: ActionsApi, org_id: str, repo_id: str
) -> dict[str, Any]:
    return await backfill_ci_history(
        engine,
        history=GitHubActionsHistory(FakeTokens(), api),
        backfill=Backfill(
            ti_unit_of_work(engine), InMemoryArtifactStore(), JUnitParser()
        ),
        org_id=org_id,
        repo_id=repo_id,
        max_runs=50,
        workflow=None,
        branch=None,
        pattern="junit-*",
    )


async def _scalar(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with transaction(engine) as session:
        return (await session.execute(text(sql), params)).scalar_one()


def test_backfill_records_original_time_and_reimport_is_a_no_op(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        owner = f"acme-{uuid.uuid4().hex[:8]}"
        org_id, repo_id = await _bind(engine, owner)
        api = _api(owner)
        first = await _backfill(engine, api, org_id, repo_id)
        again = await _backfill(engine, api, org_id, repo_id)
        async with transaction(engine) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT u.provenance, u.token_id, u.origin_run_url, u.ci_source,"
                        " u.ci_run_id, u.finished_at, r.ci_completed_at, r.received_at,"
                        " r.source FROM ti.uploads u JOIN ti.runs r ON r.upload_id = u.id"
                        " WHERE u.org_id = :org ORDER BY r.ci_completed_at"
                    ),
                    {"org": org_id},
                )
            ).all()
            events: dict[str, int] = dict(
                (
                    await session.execute(
                        text(
                            "SELECT name, count(*) FROM ti.outbox WHERE org_id = :org"
                            " GROUP BY name"
                        ),
                        {"org": org_id},
                    )
                ).all()
            )
        return owner, first, again, rows, events

    owner, first, again, rows, events = run(url, scenario)
    assert first["totals"] == {"imported": 6, "duplicate": 0, "rejected": 0}
    assert again["totals"] == {"imported": 0, "duplicate": 6, "rejected": 0}
    assert [a["run_id"] for a in again["artifacts"]] == [
        a["run_id"] for a in first["artifacts"]
    ]
    assert {a["artifact"] for a in first["artifacts"]} == {"junit-linux"}
    assert len(rows) == 6
    for day, row in enumerate(rows):
        (
            provenance,
            token,
            run_url,
            source,
            run_id,
            finished,
            completed,
            received,
            kind,
        ) = row
        assert (provenance, token, source, kind) == (
            "github_actions_backfill",
            None,
            "github_actions_backfill",
            "ci_upload",
        )
        assert run_id == str(1000 + day)
        assert (
            run_url
            == f"https://github.com/{owner}/web/actions/runs/{1000 + day}/attempts/1"
        )
        assert completed == finished == DAY0 + timedelta(days=day)
        assert received > completed + timedelta(days=30)  # receipt time stays honest
    assert events == {"ti.backfill.imported": 6, "ti.backfill.duplicate": 6}


def test_the_binding_decides_the_repository_and_scope_is_exact(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        owner = f"acme-{uuid.uuid4().hex[:8]}"
        org_id, repo_id = await _bind(engine, owner)
        api = _api(owner)
        refused = []
        for org, repo in (
            (str(uuid.uuid4()), repo_id),  # another org naming this binding
            (org_id, str(uuid.uuid4())),  # an unregistered repository
        ):
            try:
                await _backfill(engine, api, org, repo)
            except ResourceNotFound:
                refused.append(True)
        written = await _scalar(
            engine, "SELECT count(*) FROM ti.uploads WHERE repo_id = :r", r=repo_id
        )
        return refused, api.requests, written

    refused, requests, written = run(url, scenario)
    assert refused == [True, True]
    assert requests == []  # nothing was fetched for a pair without the binding
    assert written == 0


def test_analytics_and_evaluation_read_ci_time(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        owner = f"acme-{uuid.uuid4().hex[:8]}"
        org_id, repo_id = await _bind(engine, owner)
        await _backfill(engine, _api(owner), org_id, repo_id)
        async with transaction(engine) as session:
            store = SqlTiStore(session)
            early = await store.repo_history(
                org_id=org_id, repo_id=repo_id, cutoff=DAY0 + timedelta(days=2, hours=1)
            )
            trend = await store.failure_trend(
                org_id=org_id,
                repo_id=repo_id,
                start=DAY0 - timedelta(days=1),
                end=DAY0 + timedelta(days=6),
                bucket="day",
                limit=100,
            )
        report = await Evaluation(ti_unit_of_work(engine)).run(
            org_id=org_id,
            repo_id=repo_id,
            train_cutoff=DAY0 + timedelta(hours=1),
            horizon=timedelta(days=2),
            k=2,
        )
        return org_id, repo_id, early, trend, report

    org_id, repo_id, early, trend, report = run(url, scenario)
    assert {len(h.observations) for h in early} == {3}  # days 0-2 only
    assert all(
        o.received_at <= DAY0 + timedelta(days=2, hours=1)
        for h in early
        for o in h.observations
    )
    assert [cell.bucket_start.day for cell in trend] == [1, 2, 3, 4, 5, 6]
    assert [cell.flaky_test_runs for cell in trend] == [1, 0, 1, 0, 1, 0]
    assert [cell.failed_test_runs for cell in trend] == [1, 0, 1, 1, 1, 1]
    card = report.dataset
    assert (card.train_rows, card.test_rows) == (3, 3)
    assert report.baseline.status is Status.MEASURED
    assert report.candidate.reason == "not_configured"

    code = analytics.run_evaluate_prioritization(
        Settings(Secret(url)),
        [
            "--org-id", org_id,
            "--repo-id", repo_id,
            "--train-cutoff", (DAY0 + timedelta(hours=1)).isoformat(),
            "--horizon-days", "2",
            "--k", "2",
        ],
    )  # fmt: skip
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dataset"]["test_rows"] == 3
    assert printed["candidate"]["status"] == "blocked"


def test_a_ci_upload_without_a_token_is_refused_by_the_schema(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        now = datetime.now(UTC)
        async with transaction(engine) as session:
            await SqlTiStore(session).claim_upload(
                NewUpload(
                    upload_id=str(uuid.uuid4()),
                    org_id=str(uuid.uuid4()),
                    repo_id=str(uuid.uuid4()),
                    token_id=None,
                    metadata=UploadMetadata(
                        "github", "1", 1, "test", "a" * 40, "refs/heads/main",
                        "ci", now, now,
                    ),
                    content_sha256=bytes(32),
                    size_bytes=1,
                    content_type="application/xml",
                    artifact_key="k",
                )
            )  # fmt: skip

    with pytest.raises(IntegrityError, match="ck_uploads_token_provenance"):
        run(url, scenario)

