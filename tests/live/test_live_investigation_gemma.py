"""One real investigation whose hypothesize stage calls hosted Gemma (goals 1 and 2).

Owner-run only, skipped unless the Gemini key is in the environment; a skip is
not verification. Needs a throwaway PostgreSQL (`FLAKEPILOT_TEST_DATABASE_URL`):

    FLAKEPILOT_LIVE_GEMINI_API_KEY=... [FLAKEPILOT_LIVE_GEMMA_MODEL=gemma-4-31b-it] \\
    FLAKEPILOT_TEST_DATABASE_URL=postgresql://... \\
        uv run pytest -s tests/live -k investigation_gemma

Seeds an org, its admin, a registered repository and one stored flaky run;
enables the org AI policy (`evidence_only`) through `set-ai-policy`; then runs
the investigation as that admin, wired like the API process (code-host standing
from the test verifier; no research provider, so no SerpApi call). Exactly one
Gemini request leaves the process: the run's case lines as untrusted evidence.
"""

import asyncio
import os
from typing import Any

import pytest
from agent_route_support import ADMIN_GITHUB_ID, ReadVerifier, seed

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.administration import run_set_ai_policy
from flakepilot.interface.composition_root.database import create_engine
from flakepilot.interface.composition_root.temporal_investigation import (
    member_investigation,
)
from flakepilot.interface.composition_root.temporal_workflow import (
    InvestigationRequest,
)
from flakepilot.shared_kernel.events import Channel, Origin

GEMINI_KEY = os.environ.get("FLAKEPILOT_LIVE_GEMINI_API_KEY", "")
GEMMA_MODEL = os.environ.get("FLAKEPILOT_LIVE_GEMMA_MODEL") or "gemma-4-26b-a4b-it"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not GEMINI_KEY, reason="FLAKEPILOT_LIVE_GEMINI_API_KEY is not set"
    ),
]


def test_live_investigation_hypothesize_is_produced(migrated_database_url: str) -> None:
    settings = Settings(
        Secret(migrated_database_url),
        gemini_api_key=Secret(GEMINI_KEY),
        gemini_model=GEMMA_MODEL,
    )

    async def seeded() -> Any:
        engine = create_engine(migrated_database_url)
        try:
            return await seed(engine)
        finally:
            await engine.dispose()

    org = asyncio.run(seeded())
    policy = [
        "--org-id",
        org.org_id,
        "--admin-github-id",
        ADMIN_GITHUB_ID,
        "--level",
        "evidence_only",
    ]
    assert run_set_ai_policy(settings, policy) == 0

    async def investigate() -> Any:
        engine = create_engine(migrated_database_url)
        try:
            run = member_investigation(engine, settings, ReadVerifier())
            request = InvestigationRequest(
                org.run_id, org.org_id, org.run_id, ADMIN_GITHUB_ID
            )
            return await run(request, Origin(Channel.CLI, "live-gemma-investigation"))
        finally:
            await engine.dispose()

    report = asyncio.run(investigate())
    stages = {s.name.value: (s.status.value, s.reason) for s in report.stages}
    print(f"investigation stages: {stages}")
    assert report.assist is not None
    print(
        f"assist: outcome={report.assist.outcome.value} reason={report.assist.reason} "
        f"linked={report.assist.linked} unverified={report.assist.unverified}"
    )
    assert stages["hypothesize"] == ("produced", None)
    assert report.assist.outcome.value == "produced"
