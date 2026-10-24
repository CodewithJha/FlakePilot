"""The staging harness over real PostgreSQL: the real binding lookup and the real
verifier assembly with a canned provider answer, plus the production-default
(no-credentials) gate — and proof that the harness is read-only.

The provider here is canned, so these are PostgreSQL integration tests with
faked provider responses — they are NOT live GitHub verification, and the
reports they produce say so through zero provider contacts."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.integrations.application.ports import CodeHostRole
from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.interface.composition_root.settings import GitHubInstallation
from flakepilot.interface.composition_root.staging_verification import (
    RequestCounter,
    StagingReport,
    _default_directories,
    _default_verifier_factory,
    render,
    run_staging_verification,
    staging_checks,
)

pytestmark = pytest.mark.integration

ORG = str(uuid.uuid4())
REPO = str(uuid.uuid4())


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def sql(engine: AsyncEngine, statement: str, **params: object) -> int:
    """One statement against the deployment's database; commits itself."""
    async with transaction(engine) as session:
        result: Any = await session.execute(text(statement), params)
        if result.returns_rows:
            return len(list(result.mappings()))
        await session.commit()
        return 0


@pytest.fixture(scope="module")
def url(migrated_database_url: str) -> str:
    """A module-shared migrated database, like the other integration modules."""
    return migrated_database_url


def settings_with(url: str) -> Settings:
    """Both installation credentials configured; the values are never used —
    the provider is canned in every scenario here."""
    return Settings(
        Secret(url),
        github_installation=GitHubInstallation(
            app_id="0000001", private_key=Secret("unused-here")
        ),
    )


class CannedProvider:
    """The provider answer a real installation would report. No network."""

    async def repository_permission(self, *, owner: str, name: str, login: str) -> Any:
        return {
            "allowed-member": CodeHostRole.READ,
            "denied-member": CodeHostRole.NONE,
        }[login]


def test_the_harness_runs_the_real_binding_lookup_and_is_read_only(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        await sql(
            engine,
            "INSERT INTO repo.repositories (id, org_id, host, owner, name, created_at) "
            "VALUES (:r, :o, 'github', 'acme', 'web', now())",
            r=REPO,
            o=ORG,
        )
        before = await sql(engine, "SELECT count(*) FROM repo.outbox")

        counter = RequestCounter()
        checks, blockers = await staging_checks(
            settings_with(url),
            org_id=ORG,
            repo_id=REPO,
            owner="acme",
            name="web",
            allow_login="allowed-member",
            deny_login="denied-member",
            counter=counter,
            engine=engine,
            directories_factory=_default_directories,
            provider_factory=lambda _settings, _counter: CannedProvider(),
            verifier_factory=_default_verifier_factory,
        )
        after = await sql(engine, "SELECT count(*) FROM repo.outbox")
        assert after == before  # read-only: no outbox row was appended

        assert blockers == ()
        by_name = {check.name: check for check in checks}
        assert by_name["installation_credentials"].passed
        assert by_name["fail_closed_without_credentials"].passed
        assert by_name["binding_registered"].passed
        assert by_name["allow_login"].actual == "READ"
        assert by_name["deny_login"].actual == "NONE"
        assert by_name["allow_login"].provider_requests == 0

        report = StagingReport(
            label="staging-integration",
            started_at="2026-10-24T00:00:00+00:00",
            repository=f"acme/web (org {ORG}, repo {REPO})",
            checks=checks,
            blockers=blockers,
        )
        shown = render(report)
        # A canned provider is not live verification: the report says so.
        assert "LIVE VERIFICATION NOT PERFORMED" in shown
        assert "result: PASS" in shown

    run(url, scenario)


def test_the_production_default_without_credentials_denies_and_exits_nonzero(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """No GitHub App configured: the harness refuses a live run before any
    database connection, reports the blocker, and exits nonzero."""
    status = run_staging_verification(
        Settings(Secret(url)),
        [
            "--label",
            "staging-integration",
            "--org-id",
            ORG,
            "--repo-id",
            REPO,
            "--owner",
            "acme",
            "--name",
            "web",
            "--allow-login",
            "allowed-member",
            "--deny-login",
            "denied-member",
        ],
    )
    out = capsys.readouterr().out
    assert status == 1
    assert "LIVE VERIFICATION NOT PERFORMED" in out
    assert "installation credentials are not configured" in out
    assert "check fail_closed_without_credentials: PASS" in out


def test_bad_input_is_refused_by_the_real_command_gate(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status = run_staging_verification(
        settings_with(url),
        [
            "--org-id",
            "not-a-uuid",
            "--repo-id",
            REPO,
            "--owner",
            "acme",
            "--name",
            "web",
            "--allow-login",
            "allowed-member",
            "--deny-login",
            "denied-member",
        ],
    )
    out = capsys.readouterr().out
    assert status == 1
    assert "invalid_org_id" in out
