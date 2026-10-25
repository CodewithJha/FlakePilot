"""Operator commands that bind and unbind Auth0 agents (goal 9; the `set-ai-policy` pattern).

Subcommands of `python -m flakepilot.interface.composition_root`:

- `bind-agent --org-id <uuid> --admin-github-id <id> --repo-id <uuid>
  --auth0-subject <sub> --agent-name <code> --scope repo.investigate
  [--scope repo.view] --ttl-minutes <1-480>`: bind one Auth0 client (its
  token `sub`, e.g. `<client_id>@clients`) to a delegation from one org admin
  on one registered repository. Prints the binding ID and expiry.
- `unbind-agent --org-id <uuid> --admin-github-id <id> --auth0-subject <sub>`:
  revoke that subject's binding in the org; a repeat prints `not_bound`.

Trust: the operator acts on behalf of a named org admin (the only role
accepted), audited as `iam.agent.bound` / `iam.agent.unbound` with that admin
as actor. The subject is stored only as a SHA-256 and never printed back. No
code-host check happens here (ADR-015 deviation 2); the agent route verifies
the delegator's standing on every request. Errors name the problem, never a
database detail.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from flakepilot.identity.application.agent_bindings import bind_agent, unbind_agent
from flakepilot.identity.application.agents import MAX_TTL, AgentRefused
from flakepilot.identity.application.authorization import (
    Grant,
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import Iam, SessionPrincipal, uuid_ok
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.integrations.infrastructure.github import ISSUER as GITHUB_ISSUER
from flakepilot.interface.composition_root import Settings, iam_unit_of_work
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
    transaction,
)
from flakepilot.repository.infrastructure.store import SqlRepoDirectory
from flakepilot.shared_kernel.events import Channel, Origin

_PROG = "python -m flakepilot.interface.composition_root"
_MAX_MINUTES = int(MAX_TTL.total_seconds() // 60)


def _origin() -> Origin:
    return Origin(Channel.CLI, str(uuid.uuid4()))


def _parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} {command}",
        description=(
            "Bind one Auth0 client to a delegation from an org admin."
            if command == "bind-agent"
            else "Revoke one Auth0 client's binding in the org."
        ),
    )
    parser.add_argument("--org-id", required=True, help="the org's UUID")
    parser.add_argument(
        "--admin-github-id", required=True, help="numeric GitHub ID of an org admin"
    )
    parser.add_argument(
        "--auth0-subject", required=True, help="the token sub, e.g. <id>@clients"
    )
    if command == "bind-agent":
        parser.add_argument("--repo-id", required=True, help="the repository's UUID")
        parser.add_argument(
            "--agent-name", required=True, help="short code, e.g. mastra_triage"
        )
        parser.add_argument(
            "--scope",
            required=True,
            action="append",
            choices=[Grant.VIEW.value, Grant.INVESTIGATE.value],
        )
        parser.add_argument(
            "--ttl-minutes", required=True, type=int, help=f"1-{_MAX_MINUTES}"
        )
    return parser


async def bind(
    settings: Settings,
    *,
    org_id: str,
    github_id: str,
    repo_id: str,
    subject: str,
    agent_name: str,
    scopes: Sequence[str],
    ttl: timedelta,
) -> tuple[str, datetime]:
    if not uuid_ok(repo_id):  # refuse before any database access
        raise ResourceNotFound
    engine = create_engine(settings.database_url.reveal())
    try:
        iam = Iam(iam_unit_of_work(engine), identity_provider=None)
        if not await iam.org_exists(org_id):
            raise ResourceNotFound
        user_id = await iam.find_user(issuer=GITHUB_ISSUER, subject=github_id)
        if user_id is None:
            raise ResourceNotFound
        admin = await iam.org_access(SessionPrincipal(user_id), org_id)
        async with transaction(engine) as session:
            registered = await SqlRepoDirectory(session).get(repo_id)
        if registered is None or registered.org_id != org_id:
            raise ResourceNotFound
        async with transaction(engine) as session:
            return await bind_agent(
                SqlIamStore(session),
                admin=admin,
                org_id=org_id,
                repo_id=repo_id,
                subject=subject,
                agent_id=agent_name,
                scopes={Grant(scope) for scope in scopes},
                ttl=ttl,
                origin=_origin(),
            )
    finally:
        await dispose_engine(engine)


async def unbind(
    settings: Settings, *, org_id: str, github_id: str, subject: str
) -> str:
    engine = create_engine(settings.database_url.reveal())
    try:
        iam = Iam(iam_unit_of_work(engine), identity_provider=None)
        if not await iam.org_exists(org_id):
            raise ResourceNotFound
        user_id = await iam.find_user(issuer=GITHUB_ISSUER, subject=github_id)
        if user_id is None:
            raise ResourceNotFound
        admin = await iam.org_access(SessionPrincipal(user_id), org_id)
        async with transaction(engine) as session:
            return await unbind_agent(
                SqlIamStore(session),
                admin=admin,
                org_id=org_id,
                subject=subject,
                origin=_origin(),
            )
    finally:
        await dispose_engine(engine)


def _fail(command: str, message: str) -> int:
    print(f"{command}: {message}", file=sys.stderr)
    return 1


def run_bind_agent(settings: Settings, argv: Sequence[str]) -> int:
    args = _parser("bind-agent").parse_args(argv)
    if not 1 <= args.ttl_minutes <= _MAX_MINUTES:
        return _fail("bind-agent", f"--ttl-minutes must be 1-{_MAX_MINUTES}")
    try:
        binding_id, expires_at = asyncio.run(
            bind(
                settings,
                org_id=args.org_id,
                github_id=args.admin_github_id,
                repo_id=args.repo_id,
                subject=args.auth0_subject,
                agent_name=args.agent_name,
                scopes=args.scope,
                ttl=timedelta(minutes=args.ttl_minutes),
            )
        )
    except ResourceNotFound:
        return _fail("bind-agent", "no such org, admin, or registered repository")
    except PermissionDenied:
        return _fail("bind-agent", "the member is not an org admin")
    except AgentRefused as exc:
        return _fail("bind-agent", f"refused: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 - driver detail is never printed
        return _fail("bind-agent failed", type(exc).__name__)
    print(f"bound {binding_id} expires_at={expires_at.isoformat()}")
    return 0


def run_unbind_agent(settings: Settings, argv: Sequence[str]) -> int:
    args = _parser("unbind-agent").parse_args(argv)
    try:
        outcome = asyncio.run(
            unbind(
                settings,
                org_id=args.org_id,
                github_id=args.admin_github_id,
                subject=args.auth0_subject,
            )
        )
    except ResourceNotFound:
        return _fail("unbind-agent", "no such org or admin")
    except PermissionDenied:
        return _fail("unbind-agent", "the member is not an org admin")
    except AgentRefused as exc:
        return _fail("unbind-agent", f"refused: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 - driver detail is never printed
        return _fail("unbind-agent failed", type(exc).__name__)
    print(outcome)
    return 0
