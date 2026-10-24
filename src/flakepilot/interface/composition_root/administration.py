"""Operator grant-administration commands (the `bootstrap-org` trust channel).

Subcommands of `python -m flakepilot.interface.composition_root`:

- `register-repo --org-id <uuid> --repo-id <uuid> --owner <owner> --name <name>`:
  record that this deployment serves one GitHub repository under one org. The exact
  repeat (same ID and coordinates) changes nothing; a repository ID registered with
  different coordinates, coordinates registered under a different ID, or a missing org
  are errors.
- `add-member --org-id <uuid> --member-github-id <id> [--member-login <login>] [--admin]`:
  make one GitHub user an org member. The member is identified by their GitHub subject
  (the verified-identity path `bootstrap-org` uses), never by an internal user ID.
  Repeating the same membership changes nothing; a conflicting org_admin flag is an
  error (bootstrap makes the first admin; changes to it get their own flow).
- `grant --org-id <uuid> --member-github-id <id> --repo-id <uuid> --grant repo.view`:
  record one explicit `repo.*` grant for a member of this org. The member must exist
  and already be a member; the repository must be registered in this org, checked in
  this command because IAM cannot read REPO's records. Repeating is not an error.
- `revoke --org-id <uuid> --member-github-id <id> --repo-id <uuid> --grant repo.view`:
  delete the grant; a repeat is not an error.
- `show-access --org-id <uuid> --member-github-id <id> [--repo-id <uuid>]`: render a
  member's org standing through the same `decide()` rules the views use. The code-host
  standing shown is the deployment's read standing. The operator channel does not
  consult REPO's `CodeHostAccessVerifier` (ADR-015 deviation 2, owner decision
  pending), so it is NOT evidence that the member holds code-host access (D2).

Trust in one sentence: these mutations carry the operator's authority, are audited as
`operator_cli` events in their own context outboxes, and bridge the gap until
session-admin flows can safely check `may_grant()` with the verifier's verified
standing (ADR-011 I; ADR-015 deviation 2 keeps this channel's grants unverified
pending an owner decision); nothing here claims a grantee holds code-host access.
Errors name the problem and never a database detail or another org's existence.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from flakepilot.identity.application.administration import (
    GRANTS,
    AccessOverview,
    InvalidAdminCommand,
    add_member,
    grant_repo_access,
    overview_repos,
    revoke_repo_access,
    session_access_overview,
    validate_change_input,
)
from flakepilot.identity.application.authorization import ResourceNotFound
from flakepilot.identity.application.iam import Iam, SessionPrincipal
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.integrations.infrastructure.github import ISSUER as GITHUB_ISSUER
from flakepilot.interface.composition_root import Settings, iam_unit_of_work
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
    transaction,
)
from flakepilot.repository.application.registration import (
    RegistrationConflict,
    register_repository,
    validate_binding,
)
from flakepilot.repository.application.source import SourceError
from flakepilot.repository.infrastructure.registry import SqlRegistrationStore
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Channel, Origin

_DESCRIPTIONS = {
    "register-repo": "Record that this deployment serves one (org, repository) pair.",
    "add-member": "Make one GitHub user a member of the org.",
    "grant": "Record one explicit repo.* grant for an org member.",
    "revoke": "Delete one explicit repo.* grant.",
    "show-access": "Show a member's standing in the org through decide().",
}

CommandRunner = Callable[[Settings, Sequence[str]], int]


def _origin() -> Origin:
    """One correlation ID per command run, like every other CLI command."""
    return Origin(Channel.CLI, str(uuid.uuid4()))


def _parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m flakepilot.interface.composition_root {command}",
        description=_DESCRIPTIONS[command],
    )
    if command == "show-access":
        parser.add_argument("--org-id", required=True, help="the org's UUID")
        parser.add_argument(
            "--member-github-id", required=True, help="numeric GitHub user ID"
        )
        parser.add_argument(
            "--repo-id", default=None, help="report one repo (a UUID) instead of all"
        )
        return parser
    if command == "register-repo":
        parser.add_argument("--org-id", required=True, help="the org's UUID")
        parser.add_argument("--repo-id", required=True, help="the repository's UUID")
        parser.add_argument("--owner", required=True, help="GitHub repository owner")
        parser.add_argument("--name", required=True, help="GitHub repository name")
        return parser
    parser.add_argument("--org-id", required=True, help="the org's UUID")
    parser.add_argument("--member-github-id", required=True, help="numeric GitHub ID")
    if command == "add-member":
        parser.add_argument("--member-login", default=None)
        parser.add_argument(
            "--admin", action="store_true", help="make the member an org admin"
        )
        return parser
    parser.add_argument("--repo-id", required=True, help="the repository's UUID")
    parser.add_argument(
        "--grant",
        required=True,
        choices=sorted(g.value for g in GRANTS),
        help="the permission to change",
    )
    return parser


def _print_failure(context: str, message: str) -> None:
    print(f"{context}: {message}", file=sys.stderr)


async def _register_repo(
    settings: Settings, org_id: str, repo_id: str, owner: str, name: str
) -> str:
    validate_binding(org_id, repo_id, owner, name)  # refuse before any database access
    engine = create_engine(settings.database_url.reveal())
    try:
        iam = Iam(iam_unit_of_work(engine), identity_provider=None)
        if not await iam.org_exists(org_id):
            raise InvalidAdminCommand("no such org")
        async with transaction(engine) as session:
            return await register_repository(
                SqlRegistrationStore(session),
                org_id=org_id,
                repo_id=repo_id,
                owner=owner,
                name=name,
                origin=_origin(),
                now=datetime.now(UTC),
            )
    finally:
        await dispose_engine(engine)


def run_register_repo(settings: Settings, argv: Sequence[str]) -> int:
    args = _parser("register-repo").parse_args(argv)
    try:
        outcome = asyncio.run(
            _register_repo(settings, args.org_id, args.repo_id, args.owner, args.name)
        )
    except (InvalidAdminCommand, RegistrationConflict, SourceError) as exc:
        _print_failure("register-repo", f"{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - driver detail is never printed
        _print_failure("register-repo failed", type(exc).__name__)
        return 1
    print(outcome)
    return 0


async def _add_member(
    settings: Settings, org_id: str, github_id: str, login: str | None, admin: bool
) -> str:
    engine = create_engine(settings.database_url.reveal())
    try:
        async with transaction(engine) as session:
            _user_id, outcome = await add_member(
                SqlIamStore(session),
                org_id=org_id,
                member_issuer=GITHUB_ISSUER,
                member_subject=github_id,
                member_login=login,
                org_admin=admin,
                origin=_origin(),
            )
        return outcome
    finally:
        await dispose_engine(engine)


def run_add_member(settings: Settings, argv: Sequence[str]) -> int:
    args = _parser("add-member").parse_args(argv)
    try:
        outcome = asyncio.run(
            _add_member(
                settings,
                args.org_id,
                args.member_github_id,
                args.member_login,
                args.admin,
            )
        )
    except InvalidAdminCommand as exc:
        _print_failure("add-member", f"{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - driver detail is never printed
        _print_failure("add-member failed", type(exc).__name__)
        return 1
    print(outcome)
    return 0


async def _change_grant(
    settings: Settings,
    *,
    command: str,
    org_id: str,
    repo_id: str,
    github_id: str,
    grant_value: str,
) -> str:
    """Grant needs the repository binding first: the claim "this repo is this org's" is
    REPO's record, not an input. Two short transactions, each one context."""
    validate_change_input(
        org_id=org_id,
        repo_id=repo_id,
        member_subject=github_id,
        grant_value=grant_value,
    )
    engine = create_engine(settings.database_url.reveal())
    try:
        if command == "grant":
            async with transaction(engine) as session:
                binding = await SqlRepoDirectory(session).get(repo_id)
            if binding is None or binding.org_id != org_id:
                raise InvalidAdminCommand(
                    "the repository is not registered in this org"
                )
            async with transaction(engine) as session:
                return await grant_repo_access(
                    SqlIamStore(session),
                    org_id=org_id,
                    repo_id=repo_id,
                    grant_value=grant_value,
                    member_issuer=GITHUB_ISSUER,
                    member_subject=github_id,
                    origin=_origin(),
                )
        async with transaction(engine) as session:
            return await revoke_repo_access(
                SqlIamStore(session),
                org_id=org_id,
                repo_id=repo_id,
                grant_value=grant_value,
                member_issuer=GITHUB_ISSUER,
                member_subject=github_id,
                origin=_origin(),
            )
    finally:
        await dispose_engine(engine)


def _run_change_grant(command: str) -> CommandRunner:
    def command_runner(settings: Settings, argv: Sequence[str]) -> int:
        args = _parser(command).parse_args(argv)
        try:
            outcome = asyncio.run(
                _change_grant(
                    settings,
                    command=command,
                    org_id=args.org_id,
                    repo_id=args.repo_id,
                    github_id=args.member_github_id,
                    grant_value=args.grant,
                )
            )
        except InvalidAdminCommand as exc:
            _print_failure(command, f"{exc}")
            return 1
        except Exception as exc:  # noqa: BLE001 - driver detail is never printed
            _print_failure(f"{command} failed", type(exc).__name__)
            return 1
        print(outcome)
        return 0

    return command_runner


def _render(overview: AccessOverview) -> None:
    print(f"org {overview.org_id} (org_admin={str(overview.org_admin).lower()})")
    print(
        "code-host standing: deployment read "
        "(the operator channel does not consult the code-host verifier; "
        "this is not verified access)"
    )
    if not overview.repos:
        print("  no explicit grants and no repo requested")
    for repo in overview.repos:
        print(f"repo {repo.repo_id}")
        print(
            "  explicit grants: "
            + (", ".join(sorted(g.value for g in repo.explicit)) or "none")
        )
        print(
            "  effective: "
            + (", ".join(sorted(g.value for g in repo.effective)) or "none")
        )
        print("  decide(): " + " ".join(f"{g.value}={d}" for g, d in repo.decisions))


async def _show_access(
    settings: Settings, org_id: str, github_id: str, repo_id: str | None
) -> None:
    engine = create_engine(settings.database_url.reveal())
    try:
        iam = Iam(iam_unit_of_work(engine), identity_provider=None)
        user_id = await iam.find_user(issuer=GITHUB_ISSUER, subject=github_id)
        if user_id is None:
            # Same non-disclosing outcome for unknown users and non-members.
            raise ResourceNotFound
        access = await iam.org_access(SessionPrincipal(user_id), org_id)
        repo_ids = [repo_id] if repo_id is not None else overview_repos(access)
        _render(session_access_overview(access, repo_ids))
    finally:
        await dispose_engine(engine)


def run_show_access(settings: Settings, argv: Sequence[str]) -> int:
    args = _parser("show-access").parse_args(argv)
    try:
        asyncio.run(
            _show_access(settings, args.org_id, args.member_github_id, args.repo_id)
        )
    except ResourceNotFound:
        print("show-access: no such member in this org", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - driver detail is never printed
        _print_failure("show-access failed", type(exc).__name__)
        return 1
    return 0


_COMMANDS: dict[str, CommandRunner] = {
    "register-repo": run_register_repo,
    "add-member": run_add_member,
    "grant": _run_change_grant("grant"),
    "revoke": _run_change_grant("revoke"),
    "show-access": run_show_access,
}


def run_administration(settings: Settings, argv: Sequence[str]) -> int:
    """Dispatch one administration subcommand; `argv` starts with the command."""
    command, rest = argv[0], list(argv[1:])
    try:
        handler = _COMMANDS[command]
    except KeyError:
        raise SystemExit(f"Unknown command: {command}") from None
    return handler(settings, rest)
