"""Staging-only live verification of the code-host access verifier (read-only).

`python -m flakepilot.interface.composition_root verify-github-app --help` drives
the *real* composition verifier — REPO's `BoundCodeAccessVerifier` over this
deployment's `repo.repositories` binding and the real `GitHubCodeHost` provider
with the deployment's installation credentials — against an explicitly named
staging repository, plus a no-credentials fail-closed probe that must answer
unavailable without any provider contact. This is an operator diagnostic in the
bootstrap trust channel: it performs no mutation (no grant, membership, session,
audit or outbox change) and settles no policy — Decisions A–E of the
grant-authorization decision sheet stay open.

What the command guarantees:

- Live verification happens only when `FLAKEPILOT_GITHUB_APP_ID` and
  `FLAKEPILOT_GITHUB_APP_PRIVATE_KEY` are both configured (Settings refuses a
  partial configuration at bootstrap). Otherwise the report says LIVE
  VERIFICATION NOT PERFORMED, the fail-closed probe still runs, and the exit
  status is nonzero. There is no mock-success fallback anywhere.
- No repository or account is ever assumed: owner/name and the org/repo binding
  are arguments, and collaborator handles are checked only when the operator
  passes them explicitly.
- A GitHub 404 is ambiguous by the provider's own contract: the harness reports
  it as an indeterminate outcome and fails the check — never as a denial.
- Repository permission says nothing about which commit produced a run:
  revision pinning (`source_unverified` / `unchecked`) is untouched and is not
  exercised here.
- Provider contact is counted (installation-token mint included) so the report
  can show whether the network was genuinely used: an uncontacted provider
  cannot be a live outcome, and the report marks such a run as
  LIVE VERIFICATION NOT PERFORMED.
- Output carries outcomes, reasons, identifiers given on the command line, and
  counts only. It never contains a token, private key, cookie, or a provider
  response body.
"""

import argparse
import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from flakepilot.identity.application.verification import AccessVerification
from flakepilot.integrations.infrastructure.code_host import (
    CodeHostError,
    GitHubCodeHost,
)
from flakepilot.integrations.infrastructure.installation import (
    InstallationMinter,
    Transport,
    urllib_transport,
)
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
    transaction,
)
from flakepilot.repository.application.registration import validate_binding
from flakepilot.repository.application.source import SourceError
from flakepilot.repository.infrastructure.store import SqlRepoDirectory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from flakepilot.interface.composition_root.settings import Settings
    from flakepilot.repository.application.verification import (
        DirectoryFactory,
        PermissionProvider,
    )

NOT_PERFORMED = "not performed"
AMBIGUOUS_NOTE = "a refused or 404 lookup is not proof of denied access"
PROBE_OWNER = "probe"
PROBE_NAME = "probe"
"""Owner/name used only by the no-credentials fail-closed probe. The probe's
contract is to answer unavailable *before* any lookup (the token minter has no
credentials), so no request is ever sent and no repository is assumed."""


class RequestCounter:
    """Counts provider HTTP contacts (token mint and lookups). It never records
    URLs, headers or bodies: the evidence it supports is "the provider was
    contacted N times", not what was in a request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contacts = 0

    def wrap(self, inner: Transport) -> Transport:
        def counted(request: Any, limit: int) -> tuple[int, bytes]:
            with self._lock:
                self._contacts += 1
            return inner(request, limit)

        return counted

    @property
    def contacts(self) -> int:
        with self._lock:
            return self._contacts


@dataclass(frozen=True)
class StagingCheck:
    """One verification step's redacted outcome."""

    name: str
    passed: bool
    expected: str
    actual: str
    provider_requests: int | None = None


@dataclass(frozen=True)
class StagingReport:
    """The evidence report: the run's identity and every redacted outcome."""

    label: str
    started_at: str
    repository: str
    checks: tuple[StagingCheck, ...]
    blockers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks) and not self.blockers


def _live_contacts(report: StagingReport) -> int:
    return sum(
        check.provider_requests or 0
        for check in report.checks
        if check.provider_requests is not None
    )


def render(report: StagingReport) -> str:
    """The redacted report text. Identifiers shown are operator-supplied; no
    secret, token, cookie, URL or response body appears anywhere."""
    lines = [
        "code-host staging verification report",
        f"environment: {report.label}",
        f"started: {report.started_at}",
        f"deployment repository: {report.repository}",
        (
            "method: automated tests use injected transports; PostgreSQL "
            "integration tests use canned providers; only a run with real "
            "installation credentials and counted provider requests is live"
        ),
    ]
    if _live_contacts(report) == 0:
        lines.append("LIVE VERIFICATION NOT PERFORMED")
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        line = (
            f"check {check.name}: {status} (expected {check.expected}; "
            f"actual {check.actual}"
        )
        if check.provider_requests is not None:
            line += f"; provider requests: {check.provider_requests}"
        lines.append(line + ")")
    lines.append(
        "blockers: " + ("; ".join(report.blockers) if report.blockers else "none")
    )
    lines.append(f"result: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)


def _installation_check(settings: "Settings") -> StagingCheck:
    configured = settings.github_installation is not None
    return StagingCheck(
        name="installation_credentials",
        passed=configured,
        expected="both configured",
        actual=(
            "configured (values are never printed)"
            if configured
            else "absent: set FLAKEPILOT_GITHUB_APP_ID and "
            "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY together"
        ),
    )


async def _fail_closed_probe_async() -> StagingCheck:
    """The no-credentials probe: a provider that cannot mint a token must fail
    closed before any contact. The probe host is local to this command run — it
    never touches another process's credentials or any environment."""
    counter = RequestCounter()
    host = GitHubCodeHost(None, transport=counter.wrap(urllib_transport))
    try:
        role = await host.repository_permission(
            owner=PROBE_OWNER, name=PROBE_NAME, login="probe"
        )
    except CodeHostError as error:
        refused = error.unavailable and counter.contacts == 0
        return StagingCheck(
            name="fail_closed_without_credentials",
            passed=refused,
            expected="unavailable without contacting the host",
            actual=(
                f"unavailable ({error.reason}) with 0 provider requests"
                if refused
                else f"unexpected refusal {error.reason}: not the fail-closed contract"
            ),
            provider_requests=counter.contacts,
        )
    return StagingCheck(
        name="fail_closed_without_credentials",
        passed=False,
        expected="unavailable without contacting the host",
        actual=(
            f"answered {role.name} without credentials: the fail-closed "
            "contract is broken"
        ),
        provider_requests=counter.contacts,
    )


def _fail_closed_probe() -> StagingCheck:
    """Sync entry for the command's gate branches (no event loop running)."""
    return asyncio.run(_fail_closed_probe_async())


def _not_performed(group: str) -> StagingCheck:
    return StagingCheck(
        name=group, passed=False, expected="performed", actual=NOT_PERFORMED
    )


def _default_directories(engine: "AsyncEngine") -> "DirectoryFactory":
    """The deployment's real binding lookup: one short transaction per call."""

    @asynccontextmanager
    async def open() -> "AsyncIterator[SqlRepoDirectory]":
        async with transaction(engine) as session:
            yield SqlRepoDirectory(session)

    return open


def _default_provider_factory(
    settings: "Settings", counter: RequestCounter
) -> "PermissionProvider":
    """The deployment's real provider: GitHub's REST API as the installation
    token holder, with every HTTP contact counted. Credentials arrive only
    through `Settings`, which never logs them; no token survives past the
    minter's own expiry cache."""
    creds = settings.github_installation
    assert creds is not None  # run_staging_verification gates before this point
    minter = InstallationMinter(
        app_id=creds.app_id,
        private_key_pem=creds.private_key.reveal(),
        transport=counter.wrap(urllib_transport),
    )
    return GitHubCodeHost(None, transport=counter.wrap(urllib_transport), tokens=minter)


VerifierFactory = Callable[["AsyncEngine", "PermissionProvider"], Any]
DirectoriesFactory = Callable[["AsyncEngine"], "DirectoryFactory"]
ProviderFactory = Callable[["Settings", RequestCounter], "PermissionProvider"]


def _default_verifier_factory(
    engine: "AsyncEngine", provider: "PermissionProvider"
) -> Any:
    from flakepilot.repository.application.verification import BoundCodeAccessVerifier

    return BoundCodeAccessVerifier(_default_directories(engine), provider)


async def staging_checks(
    settings: "Settings",
    *,
    org_id: str,
    repo_id: str,
    owner: str,
    name: str,
    allow_login: str | None,
    deny_login: str | None,
    counter: RequestCounter,
    engine: "AsyncEngine",
    directories_factory: DirectoriesFactory,
    provider_factory: ProviderFactory,
    verifier_factory: VerifierFactory,
) -> tuple[tuple[StagingCheck, ...], tuple[str, ...]]:
    """The configured database path: installation evidence, the fail-closed
    probe, the binding check, and a live verifier check per explicitly
    configured handle. Read-only: the only queries are binding lookups and the
    only network is the provider's permission lookup (each contact counted);
    no IAM, audit or outbox row is touched."""
    checks: list[StagingCheck] = [
        _installation_check(settings),
        await _fail_closed_probe_async(),
    ]
    blockers: list[str] = []

    opens = directories_factory(engine)
    async with opens() as directory:
        binding = await directory.get(repo_id)
    if binding is None or binding.org_id != org_id:
        # By the verifier's own contract this is UNBOUND — never distinguished
        # from "unknown repo" to any caller (non-disclosure).
        checks.append(
            StagingCheck(
                name="binding_registered",
                passed=False,
                expected=f"registered in {org_id} as {owner}/{name}",
                actual="unbound (not this org's, or absent)",
            )
        )
        blockers.append("repository not registered in this org")
    elif (binding.owner, binding.name) != (owner, name):
        checks.append(
            StagingCheck(
                name="binding_registered",
                passed=False,
                expected=f"registered in {org_id} as {owner}/{name}",
                actual=f"registered as {binding.owner}/{binding.name}",
            )
        )
        blockers.append("binding coordinates differ from the requested repository")
    else:
        checks.append(
            StagingCheck(
                name="binding_registered",
                passed=True,
                expected=f"registered in {org_id} as {owner}/{name}",
                actual="registered",
            )
        )

    provider = provider_factory(settings, counter)
    verifier = verifier_factory(engine, provider)
    for group, login in (("allow_login", allow_login), ("deny_login", deny_login)):
        if login is None:
            checks.append(_not_performed(group))
            continue
        before = counter.contacts
        answer = await verifier.verify(login=login, org_id=org_id, repo_id=repo_id)
        requests = counter.contacts - before
        if group == "allow_login":
            expected = "verified_read or verified_write"
            passed = answer in (AccessVerification.READ, AccessVerification.WRITE)
        else:
            expected = "verified_none (denied)"
            passed = answer is AccessVerification.NONE
        actual = answer.name
        if answer is AccessVerification.INDETERMINATE:
            actual = f"INDETERMINATE ({AMBIGUOUS_NOTE})"
        checks.append(
            StagingCheck(
                name=group,
                passed=passed,
                expected=expected,
                actual=actual,
                provider_requests=requests,
            )
        )
    return tuple(checks), tuple(blockers)


async def _configured_report(
    settings: "Settings",
    *,
    label: str,
    org_id: str,
    repo_id: str,
    owner: str,
    name: str,
    allow_login: str | None,
    deny_login: str | None,
    started_at: str,
    directories_factory: DirectoriesFactory,
    provider_factory: ProviderFactory,
    verifier_factory: VerifierFactory,
    counter: RequestCounter,
) -> StagingReport:
    """The credentials-configured run: own engine, one pass, engine disposed.
    Runtime failures are reported as a blocker typed by exception name only;
    no driver detail is printed."""
    repository = f"{owner}/{name} (org {org_id}, repo {repo_id})"
    engine = create_engine(settings.database_url.reveal())
    try:
        try:
            checks, blockers = await staging_checks(
                settings,
                org_id=org_id,
                repo_id=repo_id,
                owner=owner,
                name=name,
                allow_login=allow_login,
                deny_login=deny_login,
                counter=counter,
                engine=engine,
                directories_factory=directories_factory,
                provider_factory=provider_factory,
                verifier_factory=verifier_factory,
            )
            return StagingReport(
                label=label,
                started_at=started_at,
                repository=repository,
                checks=checks,
                blockers=blockers,
            )
        except Exception as exc:  # noqa: BLE001 - driver detail is never printed
            return StagingReport(
                label=label,
                started_at=started_at,
                repository=repository,
                checks=(
                    _installation_check(settings),
                    StagingCheck(
                        name="fail_closed_without_credentials",
                        passed=False,
                        expected="unavailable without contacting the host",
                        actual=f"{NOT_PERFORMED} (runtime unreachable)",
                    ),
                    _not_performed("binding_registered"),
                    _not_performed("allow_login"),
                    _not_performed("deny_login"),
                ),
                blockers=(
                    f"the deployment's runtime is unreachable ({type(exc).__name__})",
                ),
            )
    finally:
        await dispose_engine(engine)


def _argument_blockers(
    org_id: str,
    repo_id: str,
    owner: str,
    name: str,
    allow: str | None,
    deny: str | None,
) -> tuple[str, ...]:
    """Refuse unusable input before any database or network access. Messages
    name the offending requirement; they never echo the value."""
    blockers: list[str] = []
    try:
        validate_binding(org_id, repo_id, owner, name)
    except SourceError as exc:
        blockers.append(str(exc))
    # Collaborator handles are deliberately NOT validated here: the verifier's
    # own contract answers unusable handles with INDETERMINATE, and the harness
    # reports that as a failed check — the same evidence a live lookup refuses.
    if allow is None:
        blockers.append("--allow-login is required for the access check")
    if deny is None:
        blockers.append("--deny-login is required for the access check")
    return tuple(blockers)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flakepilot.interface.composition_root verify-github-app",
        description=(
            "Read-only staging check of this deployment's GitHub App and the "
            "code-host access verifier (the Decision E runbook, mechanized: no "
            "mutations, no assumed repository or account, nonzero exit unless "
            "the requested live checks pass against a contacted provider)."
        ),
    )
    parser.add_argument(
        "--label", default="staging", help="environment label printed in the report"
    )
    parser.add_argument("--org-id", required=True, help="the org's UUID")
    parser.add_argument("--repo-id", required=True, help="the repository's UUID")
    parser.add_argument(
        "--owner", required=True, help="explicitly selected staging repository owner"
    )
    parser.add_argument(
        "--name", required=True, help="explicitly selected staging repository name"
    )
    parser.add_argument(
        "--allow-login",
        default=None,
        help="handle of the collaborator expected to have at least read access",
    )
    parser.add_argument(
        "--deny-login",
        default=None,
        help="handle of the collaborator expected to have no access",
    )
    return parser


def run_staging_verification(settings: "Settings", argv: Sequence[str]) -> int:
    """Dispatch `verify-github-app`; returns the process exit status."""
    args = _parser().parse_args(argv)
    started_at = datetime.now(UTC).isoformat()
    repository = f"{args.owner}/{args.name} (org {args.org_id}, repo {args.repo_id})"
    blockers = _argument_blockers(
        args.org_id,
        args.repo_id,
        args.owner,
        args.name,
        args.allow_login,
        args.deny_login,
    )
    checks = (
        _installation_check(settings),
        _fail_closed_probe(),
        _not_performed("binding_registered"),
        _not_performed("allow_login"),
        _not_performed("deny_login"),
    )

    if settings.github_installation is None:
        # Missing configuration: still evidence-producing (the fail-closed probe
        # runs without a database or credentials) and never a silent success.
        report = StagingReport(
            label=args.label,
            started_at=started_at,
            repository=repository,
            checks=checks,
            blockers=("installation credentials are not configured", *blockers),
        )
        print(render(report))
        return 1

    if blockers:
        report = StagingReport(
            label=args.label,
            started_at=started_at,
            repository=repository,
            checks=checks,
            blockers=blockers,
        )
        print(render(report))
        return 1

    report = asyncio.run(
        _configured_report(
            settings,
            label=args.label,
            org_id=args.org_id,
            repo_id=args.repo_id,
            owner=args.owner,
            name=args.name,
            allow_login=args.allow_login,
            deny_login=args.deny_login,
            started_at=started_at,
            directories_factory=_default_directories,
            provider_factory=_default_provider_factory,
            verifier_factory=_default_verifier_factory,
            counter=RequestCounter(),
        )
    )
    print(render(report))
    # A live run was requested: success requires the report to pass AND the
    # provider to have been genuinely contacted (a passing run with no provider
    # contact is a defect, not a live verification).
    return 0 if report.passed and _live_contacts(report) > 0 else 1
