"""The `verify-github-app` read-only staging harness (composition root).

Fakes stand in for the engine-bound directory and the provider while the real
assembly order runs as deployed: argument gate, installation gate, fail-closed
probe, binding check, then per-handle verifier checks. These are automated
checks with injected transports — **not** live verification, and the reports
they produce say so (provider requests 0 → LIVE VERIFICATION NOT PERFORMED).
No test contacts GitHub and none mutates data.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from support import FAKE_DSN, SETTINGS

from flakepilot.integrations.application.ports import CodeHostError, CodeHostRole
from flakepilot.integrations.infrastructure.code_host import GitHubCodeHost
from flakepilot.interface.composition_root import Secret, Settings, SettingsError
from flakepilot.interface.composition_root.settings import GitHubInstallation
from flakepilot.interface.composition_root.staging_verification import (
    RequestCounter,
    StagingCheck,
    StagingReport,
    render,
    run_staging_verification,
    staging_checks,
)
from flakepilot.repository.application.source import RepoBinding
from flakepilot.repository.application.verification import BoundCodeAccessVerifier

ORG = "11111111-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
UNKNOWN = "99999999-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_ORG = "22222222-2222-4333-8444-555555555555"
BINDING = RepoBinding(ORG, REPO, "acme", "web")
ENGINE = cast(AsyncEngine, object())
# A settings object like the one a configured staging deployment has: both
# installation credentials present. Values are never used; the provider is
# always injected here.
CONFIGURED = Settings(
    Secret(FAKE_DSN),
    github_installation=GitHubInstallation(
        app_id="0000001", private_key=Secret("unused-here")
    ),
)


class Directory:
    """REPO's binding contract, canned."""

    def __init__(self, *bindings: RepoBinding) -> None:
        self._bindings = {binding.repo_id: binding for binding in bindings}
        self.lookups: list[str] = []

    async def get(self, repo_id: str) -> RepoBinding | None:
        self.lookups.append(repo_id)
        return self._bindings.get(repo_id)


class Provider:
    """INT's permission provider contract: roles, or refused lookups."""

    def __init__(self, answers: dict[str, CodeHostRole | CodeHostError]) -> None:
        self._answers = answers

    async def repository_permission(
        self, *, owner: str, name: str, login: str
    ) -> CodeHostRole:
        answer = self._answers[login]
        if isinstance(answer, CodeHostError):
            raise answer
        return answer


class Refusing:
    """Fails any provider contact the flow attempts."""

    async def repository_permission(self, **kwargs: Any) -> CodeHostRole:
        raise AssertionError("a provider lookup ran")


def directories(*bindings: RepoBinding) -> Any:
    @asynccontextmanager
    async def open() -> AsyncIterator[Directory]:
        yield Directory(*bindings)

    return open


def ingest(
    checks: tuple[StagingCheck, ...],
    blockers: tuple[str, ...] = (),
) -> StagingReport:
    return StagingReport(
        label="test",
        started_at="2026-10-24T00:00:00+00:00",
        repository=f"acme/web (org {ORG}, repo {REPO})",
        checks=checks,
        blockers=blockers,
    )


async def checked(
    *,
    bindings: tuple[RepoBinding, ...] = (BINDING,),
    answers: dict[str, CodeHostRole | CodeHostError] | None = None,
    allow: str | None = "ally",
    deny: str | None = "denied",
    settings: Settings = CONFIGURED,
) -> StagingReport:
    opened = directories(*bindings)
    provider: Any = Provider(
        answers or {"ally": CodeHostRole.READ, "denied": CodeHostRole.NONE}
    )
    checks, blockers = await staging_checks(
        settings,
        org_id=ORG,
        repo_id=REPO,
        owner="acme",
        name="web",
        allow_login=allow,
        deny_login=deny,
        counter=RequestCounter(),
        engine=ENGINE,
        directories_factory=lambda _engine: opened,
        provider_factory=lambda _settings, _counter: provider,
        verifier_factory=lambda _engine, _p: BoundCodeAccessVerifier(opened, provider),
    )
    return ingest(checks, blockers)


def find(report: StagingReport, name: str) -> StagingCheck:
    return next(check for check in report.checks if check.name == name)


def test_a_verified_allow_and_deny_pass_and_are_marked_not_live() -> None:
    report = asyncio.run(checked())
    assert report.passed and report.blockers == ()
    assert find(report, "allow_login").actual == "READ"
    assert find(report, "deny_login").actual == "NONE"
    # Injected providers contact nothing: the report must not claim live runs.
    text = render(report)
    assert "LIVE VERIFICATION NOT PERFORMED" in text
    assert "result: PASS" in text


def test_the_fail_closed_probe_passes_in_a_clean_run() -> None:
    report = asyncio.run(checked())
    probe = find(report, "fail_closed_without_credentials")
    assert probe.passed
    assert "with 0 provider requests" in probe.actual
    assert probe.provider_requests == 0


def test_an_unexpected_deny_answer_fails_the_deny_check() -> None:
    report = asyncio.run(
        checked(answers={"ally": CodeHostRole.READ, "denied": CodeHostRole.WRITE})
    )
    assert not find(report, "deny_login").passed
    assert find(report, "deny_login").actual == "WRITE"
    assert not report.passed


def test_a_404_is_ambiguous_and_never_a_denial() -> None:
    """A refused or missing lookup is "cannot say". The harness fails the run
    instead of mislabelling it, in the provider's own contract's words."""
    report = asyncio.run(
        checked(
            answers={
                "ally": CodeHostRole.READ,
                "denied": CodeHostError("access_unverified", unavailable=False),
            }
        )
    )
    deny = find(report, "deny_login")
    assert not deny.passed
    assert "INDETERMINATE" in deny.actual
    assert "not proof of denied access" in deny.actual


def test_an_unavailable_or_malformed_provider_is_never_a_result() -> None:
    for error in (
        CodeHostError("access_unverified", unavailable=True),
        CodeHostError("source_unverified", unavailable=True),
    ):
        report = asyncio.run(
            checked(answers={"ally": CodeHostRole.READ, "denied": error})
        )
        deny = find(report, "deny_login")
        assert not deny.passed
        assert deny.actual == "UNAVAILABLE"
        assert not report.passed


def test_an_unbound_repo_never_meets_the_provider() -> None:
    """Unknown or foreign-org repo: the verifier returns UNBOUND without a
    provider call (non-disclosure), the harness reports the setup blocker."""
    provider: Any = Refusing()
    for opened in (
        directories(RepoBinding(OTHER_ORG, REPO, "acme", "web")),
        directories(),
    ):

        async def scenario(opened: Any = opened) -> StagingReport:
            checks, blockers = await staging_checks(
                SETTINGS,
                org_id=ORG,
                repo_id=REPO,
                owner="acme",
                name="web",
                allow_login="ally",
                deny_login="denied",
                counter=RequestCounter(),
                engine=ENGINE,
                directories_factory=lambda _engine: opened,
                provider_factory=lambda _settings, _counter: provider,
                verifier_factory=lambda _engine, _p: BoundCodeAccessVerifier(
                    opened, provider
                ),
            )
            return ingest(checks, blockers)

        report = asyncio.run(scenario())
        assert not find(report, "binding_registered").passed
        assert find(report, "allow_login").actual == "UNBOUND"
        assert find(report, "deny_login").actual == "UNBOUND"
        assert report.blockers and not report.passed


def test_a_coordinate_mismatch_is_a_setup_blocker() -> None:
    opened = directories(RepoBinding(ORG, REPO, "evil", "other"))

    async def scenario() -> StagingReport:
        provider: Any = Provider(
            {"ally": CodeHostRole.READ, "denied": CodeHostRole.NONE}
        )
        checks, blockers = await staging_checks(
            SETTINGS,
            org_id=ORG,
            repo_id=REPO,
            owner="acme",
            name="web",
            allow_login="ally",
            deny_login="denied",
            counter=RequestCounter(),
            engine=ENGINE,
            directories_factory=lambda _engine: opened,
            provider_factory=lambda _settings, _counter: provider,
            verifier_factory=lambda _engine, _p: BoundCodeAccessVerifier(
                opened, provider
            ),
        )
        return ingest(checks, blockers)

    report = asyncio.run(scenario())
    binding = find(report, "binding_registered")
    assert not binding.passed and "registered as evil/other" in binding.actual
    assert report.blockers and not report.passed


def test_reports_carry_no_secrets() -> None:
    report = asyncio.run(checked())
    shown = render(report) + repr(report)
    assert "token" not in shown.replace("permission", "")
    assert "BEGIN" not in shown and "ghs_" not in shown
    assert "cookie" not in shown


def test_a_missing_configuration_reports_not_performed_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = [
        "--label",
        "ci",
        "--org-id",
        ORG,
        "--repo-id",
        REPO,
        "--owner",
        "acme",
        "--name",
        "web",
        "--allow-login",
        "ally",
        "--deny-login",
        "denied",
    ]
    status = run_staging_verification(SETTINGS, argv)
    out = capsys.readouterr().out
    assert status == 1
    assert "LIVE VERIFICATION NOT PERFORMED" in out
    assert "installation credentials are not configured" in out
    assert "check fail_closed_without_credentials: PASS" in out


def test_a_partial_configuration_is_refused_together() -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_environ(
            {
                "FLAKEPILOT_DATABASE_URL": "postgresql://u:p@h.example/db",
                "FLAKEPILOT_GITHUB_APP_ID": "0000001",
            }
        )
    message = str(excinfo.value)
    assert "FLAKEPILOT_GITHUB_APP_ID" in message
    assert "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY" in message
    assert "0000001" not in message


def test_missing_required_arguments_exit_as_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_staging_verification(SETTINGS, [])
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_a_configured_run_over_an_unreachable_runtime_is_a_typed_blocker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        Secret(FAKE_DSN),
        github_installation=GitHubInstallation(
            app_id="0000001", private_key=Secret("unused-here")
        ),
    )
    argv = [
        "--org-id",
        ORG,
        "--repo-id",
        REPO,
        "--owner",
        "acme",
        "--name",
        "web",
        "--allow-login",
        "ally",
        "--deny-login",
        "denied",
    ]
    status = run_staging_verification(settings, argv)
    out = capsys.readouterr().out
    assert status == 1
    assert "the deployment's runtime is unreachable (" in out
    assert "check allow_login: FAIL" in out


def test_bad_arguments_block_the_configured_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        Secret(FAKE_DSN),
        github_installation=GitHubInstallation(
            app_id="0000001", private_key=Secret("unused-here")
        ),
    )
    status = run_staging_verification(
        settings,
        [
            "--org-id",
            ORG,
            "--repo-id",
            REPO,
            "--owner",
            "bad owner",
            "--name",
            "web",
            "--allow-login",
            "ally",
            "--deny-login",
            "denied",
        ],
    )
    out = capsys.readouterr().out
    assert status == 1
    assert "check allow_login: FAIL" in out and "blockers: " in out
    assert "invalid_owner" in out


def test_provider_contacts_are_counted_and_the_token_never_leaks() -> None:
    class Recording:
        def __init__(self) -> None:
            self.records: list[str] = []

        def __call__(self, request: Any, limit: int) -> tuple[int, bytes]:
            self.records.append(request.full_url)
            return 200, b'{"permission": "read"}'

    transport = Recording()
    counter = RequestCounter()

    class Source:
        async def token(self, *, owner: str, name: str) -> str:
            return "ghs_" + "a" * 20

    host = GitHubCodeHost(None, transport=counter.wrap(transport), tokens=Source())
    role = asyncio.run(
        host.repository_permission(owner="acme", name="web", login="ally")
    )
    assert role is CodeHostRole.READ
    assert counter.contacts == 1
    assert transport.records and "ghs_" not in transport.records[0]
