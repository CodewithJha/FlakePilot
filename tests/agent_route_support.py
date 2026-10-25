"""Seeding for the agent-route tests on real PostgreSQL: an org with one admin, one
registered repository, and one stored flaky run, each through its context's own
write path. Also the code-host stand-in those tests use (no GitHub App exists
here): a verifier answering `verified_read` for every lookup."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.identity.application.iam import Iam
from flakepilot.identity.application.verification import AccessVerification
from flakepilot.integrations.infrastructure.github import ISSUER as GITHUB_ISSUER
from flakepilot.interface.composition_root import iam_unit_of_work
from flakepilot.interface.composition_root.database import transaction
from flakepilot.repository.application.registration import register_repository
from flakepilot.repository.infrastructure.registry import SqlRegistrationStore
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.store import NewUpload, RunSummary
from flakepilot.test_intelligence.domain.results import (
    Outcome,
    RunSource,
    TestIdentity,
    TestResult,
    UploadMetadata,
)
from flakepilot.test_intelligence.infrastructure.store import SqlTiStore

ADMIN_GITHUB_ID = "4242"
ORIGIN = Origin(Channel.CLI, "agent-route-seed")
FLAKY = TestIdentity("tests.payment", "test_cart_totals_under_retry")


@dataclass(frozen=True)
class Seeded:
    org_id: str
    repo_id: str
    run_id: str
    admin_id: str


class ReadVerifier:
    """Stand-in for REPO's GitHub-backed verifier: always `verified_read`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def verify(
        self, *, login: str, org_id: str, repo_id: str
    ) -> AccessVerification:
        self.calls.append((org_id, repo_id))
        return AccessVerification.READ


async def seed_run(engine: AsyncEngine, *, org_id: str, repo_id: str) -> str:
    """One CI upload with one flaky test (failed, then passed on retry)."""
    run_id, upload_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(UTC)
    async with transaction(engine) as session:
        store = SqlTiStore(session)
        ids = await store.ensure_identities(
            org_id=org_id, repo_id=repo_id, identities=[FLAKY]
        )
        claimed = await store.claim_upload(
            NewUpload(
                upload_id=upload_id,
                org_id=org_id,
                repo_id=repo_id,
                token_id=str(uuid.uuid4()),
                metadata=UploadMetadata(
                    ci_source="github",
                    ci_run_id="run-1",
                    ci_attempt=1,
                    job_name="test",
                    commit_sha="a" * 40,
                    ref="refs/heads/main",
                    environment="ci-linux",
                    started_at=now,
                    finished_at=now,
                ),
                content_sha256=bytes(32),
                size_bytes=1,
                content_type="application/xml",
                artifact_key=f"{org_id}/{repo_id}/{upload_id}.xml",
            )
        )
        assert claimed
        await store.create_run(
            run_id=run_id,
            upload_id=upload_id,
            org_id=org_id,
            repo_id=repo_id,
            source=RunSource.CI_UPLOAD,
            commit_sha="a" * 40,
            environment="ci-linux",
            summary=RunSummary(
                tests=1, attempts=2, passed=1, failed=1, errored=0, skipped=0
            ),
        )
        await store.add_results(
            org_id=org_id,
            repo_id=repo_id,
            run_id=run_id,
            results=[
                (
                    ids[FLAKY],
                    TestResult(
                        identity=FLAKY, attempts=(Outcome.FAILED, Outcome.PASSED)
                    ),
                )
            ],
        )
    return run_id


async def seed(engine: AsyncEngine) -> Seeded:
    iam = Iam(iam_unit_of_work(engine), identity_provider=None)
    org_id = await iam.bootstrap_org(
        name=f"agent-route-{uuid.uuid4().hex[:8]}",
        admin_issuer=GITHUB_ISSUER,
        admin_subject=ADMIN_GITHUB_ID,
        admin_login="agent-route-admin",
        origin=ORIGIN,
    )
    admin_id = await iam.find_user(issuer=GITHUB_ISSUER, subject=ADMIN_GITHUB_ID)
    assert admin_id is not None
    repo_id = str(uuid.uuid4())
    async with transaction(engine) as session:
        await register_repository(
            SqlRegistrationStore(session),
            org_id=org_id,
            repo_id=repo_id,
            owner="flakepilot-demo",
            name=f"repo-{repo_id[:8]}",
            origin=ORIGIN,
            now=datetime.now(UTC),
        )
    run_id = await seed_run(engine, org_id=org_id, repo_id=repo_id)
    return Seeded(org_id, repo_id, run_id, admin_id)
