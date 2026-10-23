"""Grant administration for the operator CLI (ADR-011, IAM-DATA-MODEL).

The operator is the same trust `bootstrap-org` already establishes: one human command
channel that creates the org's first admin. These commands extend that channel to
members and grants, because ongoing session-admin administration cannot safely authorize
yet: per ADR-011 I a grant requires evidence that the *grantee* holds matching code-host
access, and no code-host verifier exists (`source_unverified` / `unchecked` everywhere).
Operations here therefore never claim a grantee's code-host access; the intersection is
still enforced by `decide()` at decision time, so a wrongly issued `repo.view` grant
becomes inert the moment a real verifier replaces the deployment's read standing. A
future session-admin flow must check `may_grant()` before creating rows.

Every mutation is one unit of work: the change, then its audit event, then the
session-revocation invariant (any membership or grant change revokes the affected
user's active sessions with `privilege_change`, in the same transaction). A repeat that
changes nothing appends nothing.

Audit events: `iam.member.added`, `iam.grant.granted`, `iam.grant.revoked`, plus
`iam.session.revoked` revocations; the actor is always `operator_cli`, so no caller can
act as a user here.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
)
from flakepilot.identity.application.iam import (
    _LOGIN,
    _PROVIDER_SUBJECT,
    OPERATOR_CLI_ISSUER_ID,
    IamStore,
    uuid_ok,
)
from flakepilot.identity.domain.access import OrgAccess, decide, effective_grants
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject

__all__ = [
    "GRANTS",
    "AccessOverview",
    "InvalidAdminCommand",
    "RepoOverview",
    "access_overview",
    "add_member",
    "grant_repo_access",
    "overview_repos",
    "revoke_repo_access",
]

GRANTS = (Grant.VIEW, Grant.INVESTIGATE, Grant.APPROVE, Grant.CONFIGURE)

# The `granted_by` column holds this value for operator-made rows, like `created_by`
# on operator-issued CI upload tokens: a stable, visibly non-user UUID (no foreign
# key applies). The audit event carries the true actor.
OPERATOR_GRANTER_ID = OPERATOR_CLI_ISSUER_ID


class InvalidAdminCommand(ValueError):
    """The command input is unusable and nothing was written. Messages name the field or
    cause and never include the offending value."""


def _require_uuid(value: str, label: str) -> None:
    if not uuid_ok(value):
        raise InvalidAdminCommand(f"{label} must be a canonical lowercase UUID")


def _require_member(member_subject: str, member_login: str | None) -> None:
    if not _PROVIDER_SUBJECT.fullmatch(member_subject):
        raise InvalidAdminCommand("--member-github-id must be a positive number")
    if member_login is not None and not _LOGIN.fullmatch(member_login):
        raise InvalidAdminCommand("--member-login is not a valid GitHub login")


def _parse_grant(value: str) -> Grant:
    try:
        return Grant(value)
    except ValueError:
        raise InvalidAdminCommand(
            "--grant must be one of " + ", ".join(sorted(g.value for g in GRANTS))
        ) from None


def validate_change_input(
    *, org_id: str, repo_id: str, member_subject: str, grant_value: str
) -> Grant:
    """Refuse unusable grant-mutation input before any database access (the grant
    command reads REPO's binding record first, so it must not connect with junk
    input). Returns the parsed grant."""
    _require_uuid(org_id, "--org-id")
    _require_uuid(repo_id, "--repo-id")
    _require_member(member_subject, None)
    return _parse_grant(grant_value)


def _sessions_revoked(actor: Actor, origin: Origin, user_id: str, count: int) -> Event:
    """No session ID or hash in events; the sessions are named only by their owner."""
    return Event(
        "iam.session.revoked",
        actor,
        origin,
        Subject("user", user_id),
        before="active",
        after="revoked",
        reason="privilege_change",
        payload={"sessions": count},
    )


async def _revoke_if_any(
    store: IamStore, actor: Actor, origin: Origin, user_id: str
) -> None:
    """Session-revocation invariant for membership and grant changes (ADR-011 B)."""
    revoked = await store.revoke_user_sessions(
        user_id=user_id, reason="privilege_change"
    )
    if revoked:
        await store.append_event(_sessions_revoked(actor, origin, user_id, revoked))


async def add_member(
    store: IamStore,
    *,
    org_id: str,
    member_issuer: str,
    member_subject: str,
    member_login: str | None,
    org_admin: bool,
    origin: Origin,
) -> tuple[str, str]:
    """Make a GitHub user a member of the org, in one unit of work.

    The member is identified by (issuer, subject), the same verified-identity path
    `bootstrap-org` uses for the first admin; a raw caller-supplied user ID is never
    proof of identity. Returns `(user_id, outcome)`: "added" (membership written, event
    appended, sessions revoked) or "already_member" (identical membership already held:
    nothing written, no event). Refuses to flip an org_admin status (bootstrap makes the
    first admin; later changes need their own audited flow). Raises
    `InvalidAdminCommand` without writing anything.
    """
    _require_uuid(org_id, "--org-id")
    _require_member(member_subject, member_login)
    operator = Actor(ActorType.OPERATOR_CLI)
    if not await store.org_exists(org_id=org_id):
        raise InvalidAdminCommand("no such org")
    user_id, _created = await store.ensure_user(
        issuer=member_issuer, subject=member_subject, login=member_login
    )
    existing = await store.org_access(user_id=user_id, org_id=org_id)
    if existing is not None:
        if existing.org_admin == org_admin:
            return user_id, "already_member"
        raise InvalidAdminCommand(
            "the user is already a member with a different admin status"
        )
    await store.add_membership(org_id=org_id, user_id=user_id, org_admin=org_admin)
    await store.append_event(
        Event(
            "iam.member.added",
            operator,
            origin,
            Subject("user", user_id),
            org_id=org_id,
            after="member",
            payload={"org_admin": org_admin},
        )
    )
    await _revoke_if_any(store, operator, origin, user_id)
    return user_id, "added"


async def grant_repo_access(
    store: IamStore,
    *,
    org_id: str,
    repo_id: str,
    grant_value: str,
    member_issuer: str,
    member_subject: str,
    origin: Origin,
) -> str:
    """Record one explicit `repo.*` grant for an org member, in one unit of work.

    Returns "granted" (row written, `iam.grant.granted` appended, the member's sessions
    revoked) or "already_held" (exactly this grant already held: nothing written). Raises
    `InvalidAdminCommand` without writing when the org, membership, user or grant value
    is unusable. The org foreign key keeps a grant to a non-member impossible under any
    race; cross-org repository references are refused by the operator command that reads
    the repository binding (REPO context), since IAM cannot read REPO's records.
    """
    _require_uuid(org_id, "--org-id")
    _require_uuid(repo_id, "--repo-id")
    grant = _parse_grant(grant_value)
    operator = Actor(ActorType.OPERATOR_CLI)
    if not await store.org_exists(org_id=org_id):
        raise InvalidAdminCommand("no such org")
    user_id = await store.find_user(issuer=member_issuer, subject=member_subject)
    if user_id is None:
        raise InvalidAdminCommand("no such user; add them to the org first")
    if await store.org_access(user_id=user_id, org_id=org_id) is None:
        raise InvalidAdminCommand("the user is not a member of this org")
    if not await store.add_grant(
        org_id=org_id,
        user_id=user_id,
        repo_id=repo_id,
        grant=grant,
        granted_by=OPERATOR_GRANTER_ID,
    ):
        return "already_held"
    await store.append_event(
        Event(
            "iam.grant.granted",
            operator,
            origin,
            Subject("user", user_id),
            org_id=org_id,
            repo_id=repo_id,
            after="held",
            payload={"grant": grant.value},
        )
    )
    await _revoke_if_any(store, operator, origin, user_id)
    return "granted"


async def revoke_repo_access(
    store: IamStore,
    *,
    org_id: str,
    repo_id: str,
    grant_value: str,
    member_issuer: str,
    member_subject: str,
    origin: Origin,
) -> str:
    """Delete one explicit `repo.*` grant, in one unit of work.

    Returns "revoked" (row deleted, `iam.grant.revoked` appended, sessions revoked) or
    "not_held" (nothing to delete; a repeat is not an error). Raises
    `InvalidAdminCommand` without writing when IDs or the grant value are unusable, or
    the org or user is unknown.
    """
    _require_uuid(org_id, "--org-id")
    _require_uuid(repo_id, "--repo-id")
    grant = _parse_grant(grant_value)
    operator = Actor(ActorType.OPERATOR_CLI)
    if not await store.org_exists(org_id=org_id):
        raise InvalidAdminCommand("no such org")
    user_id = await store.find_user(issuer=member_issuer, subject=member_subject)
    if user_id is None:
        raise InvalidAdminCommand("no such user")
    if not await store.revoke_grant(
        org_id=org_id, user_id=user_id, repo_id=repo_id, grant=grant
    ):
        return "not_held"
    await store.append_event(
        Event(
            "iam.grant.revoked",
            operator,
            origin,
            Subject("user", user_id),
            org_id=org_id,
            repo_id=repo_id,
            before="held",
            after="revoked",
            payload={"grant": grant.value},
        )
    )
    await _revoke_if_any(store, operator, origin, user_id)
    return "revoked"


@dataclass(frozen=True)
class RepoOverview:
    """One repo's standing for one verified org member: explicit grants, the effective
    set with implications under a code-host standing, and `decide()` per supported
    grant. Pure domain rules; display only."""

    repo_id: str
    explicit: frozenset[Grant]
    effective: frozenset[Grant]
    decisions: tuple[tuple[Grant, str], ...]


@dataclass(frozen=True)
class AccessOverview:
    """An operator-readable summary of one member's org standing."""

    org_id: str
    org_admin: bool
    repos: tuple[RepoOverview, ...]
    code_host: CodeHostAccess


def access_overview(
    access: OrgAccess, repo_ids: Sequence[str], code_host: CodeHostAccess
) -> AccessOverview:
    """Walk the member's standing per repo through the same domain rules `decide()` uses.
    Repos named with no grants are still listed, so the operator sees an empty standing
    too. `code_host` is the caller's standing — today the deployment's read standing,
    since a per-user verifier does not exist yet (never presented as verified access).
    """
    repos: list[RepoOverview] = []
    for repo_id in repo_ids:
        explicit = frozenset(g for repo, g in access.grants if repo == repo_id)
        repos.append(
            RepoOverview(
                repo_id=repo_id,
                explicit=explicit,
                effective=effective_grants(access, repo_id, code_host),
                decisions=tuple(
                    (
                        grant,
                        decide(
                            access,
                            grant,
                            org_id=access.org_id,
                            repo_id=repo_id,
                            code_host=code_host,
                        ).value,
                    )
                    for grant in GRANTS
                ),
            )
        )
    return AccessOverview(
        org_id=access.org_id,
        org_admin=access.org_admin,
        repos=tuple(repos),
        code_host=code_host,
    )


def session_access_overview(
    access: OrgAccess, repo_ids: Sequence[str]
) -> AccessOverview:
    """The overview as any session sees it: the deployment's read standing, since a
    per-user verifier does not exist yet (ADR-011 Decision I). The standing is chosen
    here, in the application layer, so composition code never supplies code-host
    permission itself. It is never presented as verified access.
    """
    return access_overview(access, repo_ids, CodeHostAccess.READ)


def overview_repos(access: OrgAccess) -> list[str]:
    """Repo IDs with explicit grants, stable order for display."""
    seen: list[str] = []
    for repo_id, _grant in sorted(access.grants):
        if repo_id not in seen:
            seen.append(repo_id)
    return seen
