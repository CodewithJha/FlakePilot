"""Live GitHub Actions backfill, then one evaluation (goal 12). Owner-run only.

Skipped unless the GitHub App credentials and a repository are in the
environment, so the default suite and CI contact nothing. A skip is not
verification. Needs a throwaway PostgreSQL (`FLAKEPILOT_TEST_DATABASE_URL`,
like every integration test) and the App installed on the repository with
actions:read:

    FLAKEPILOT_TEST_DATABASE_URL=postgresql://... \
    FLAKEPILOT_LIVE_GITHUB_APP_ID=... \
    FLAKEPILOT_LIVE_GITHUB_APP_PRIVATE_KEY_FILE=/path/key.pem \
    FLAKEPILOT_LIVE_BACKFILL_REPO=owner/name \
    [FLAKEPILOT_LIVE_BACKFILL_ARTIFACTS='*junit*'] [FLAKEPILOT_LIVE_BACKFILL_RUNS=50] \
    [FLAKEPILOT_LIVE_TABPFN_API_KEY=...] \
    uv run pytest -s tests/live -k backfill

It reads at most 50 completed runs, imports their JUnit artifacts into a
throwaway org, re-imports to prove idempotency, and runs the evaluation once
over three equal windows of the imported CI time. TabPFN is called at most
once, only with its live key (numeric feature rows and 0/1 labels leave the
process, nothing else). The report is printed; numbers mean something only if
both the baseline and the candidate are `measured`.
"""

import asyncio
import json
import os
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from support import InMemoryArtifactStore

from flakepilot.integrations.infrastructure.github_actions import GitHubActionsHistory
from flakepilot.integrations.infrastructure.hosted_http import call
from flakepilot.integrations.infrastructure.installation import InstallationMinter
from flakepilot.integrations.infrastructure.junit import JUnitParser
from flakepilot.interface.composition_root import ti_unit_of_work
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.operator import backfill_ci_history
from flakepilot.repository.application.source import new_binding
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.test_intelligence.application.backfill import Backfill
from flakepilot.test_intelligence.application.evaluation import Evaluation
from flakepilot.test_intelligence.infrastructure.tabpfn import TabPfnPredictor

APP_ID = os.environ.get("FLAKEPILOT_LIVE_GITHUB_APP_ID", "")
KEY_FILE = os.environ.get("FLAKEPILOT_LIVE_GITHUB_APP_PRIVATE_KEY_FILE", "")
REPO = os.environ.get("FLAKEPILOT_LIVE_BACKFILL_REPO", "")
PATTERN = os.environ.get("FLAKEPILOT_LIVE_BACKFILL_ARTIFACTS", "*")
RUNS = min(int(os.environ.get("FLAKEPILOT_LIVE_BACKFILL_RUNS", "50")), 50)
TABPFN_KEY = os.environ.get("FLAKEPILOT_LIVE_TABPFN_API_KEY", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (APP_ID and KEY_FILE and REPO),
        reason="FLAKEPILOT_LIVE_GITHUB_APP_ID, _PRIVATE_KEY_FILE and "
        "FLAKEPILOT_LIVE_BACKFILL_REPO are not set",
    ),
]


def test_live_backfill_is_idempotent_and_feeds_one_evaluation(
    migrated_database_url: str,
) -> None:
    owner, name = REPO.split("/", 1)
    history = GitHubActionsHistory(
        InstallationMinter(
            app_id=APP_ID,
            private_key_pem=Path(KEY_FILE).read_text(),
            scope="actions",
        )
    )

    async def scenario() -> tuple[Any, Any, Any, Any]:
        engine = create_engine(migrated_database_url)
        try:
            org_id = str(uuid.uuid4())
            binding = new_binding(org_id=org_id, owner=owner, name=name)
            async with transaction(engine) as session:
                await SqlRepoDirectory(session).add(binding, now=datetime.now(UTC))
            backfill = Backfill(
                ti_unit_of_work(engine), InMemoryArtifactStore(), JUnitParser()
            )
            args: dict[str, Any] = {
                "history": history,
                "backfill": backfill,
                "org_id": org_id,
                "repo_id": binding.repo_id,
                "max_runs": RUNS,
                "workflow": None,
                "branch": None,
                "pattern": PATTERN,
            }
            first = await backfill_ci_history(engine, **args)
            again = await backfill_ci_history(engine, **args)
            async with transaction(engine) as session:
                span = (
                    await session.execute(
                        text(
                            "SELECT min(ci_completed_at), max(ci_completed_at),"
                            " count(*) FROM ti.runs WHERE org_id = :org"
                        ),
                        {"org": org_id},
                    )
                ).one()
            report = None
            if span[2] and span[1] - span[0] >= timedelta(days=3):
                window = max(timedelta(days=1), (span[1] - span[0]) / 3)
                horizon = timedelta(days=max(1, window.days))
                predictor = (
                    TabPfnPredictor(
                        api_key=TABPFN_KEY, transport=call, timeout_seconds=600
                    )
                    if TABPFN_KEY
                    else None
                )
                report = await Evaluation(ti_unit_of_work(engine)).run(
                    org_id=org_id,
                    repo_id=binding.repo_id,
                    train_cutoff=span[0] + horizon,
                    horizon=horizon,
                    k=10,
                    predictor=predictor,
                )
            return first, again, span, report
        finally:
            await engine.dispose()

    first, again, span, report = asyncio.run(scenario())
    print(json.dumps({"first": first["totals"], "again": again["totals"]}))
    print(
        json.dumps(
            {"runs": span[2], "earliest": span[0], "latest": span[1]}, default=str
        )
    )
    print(json.dumps(asdict(report) if report else None, indent=2, default=str))
    assert first["totals"]["imported"] >= 1, first["artifacts"][:5]
    assert again["totals"]["imported"] == 0
    assert again["totals"]["duplicate"] == first["totals"]["imported"]
