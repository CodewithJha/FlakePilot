"""Stored agent bindings: which Auth0 client may act on which delegation (goal 9).

A binding is the one durable piece of the delegated-agent design: an org admin
names an Auth0 client (the token `sub`, e.g. `<client_id>@clients`) and the
delegation it may use — one repository, scopes within `{repo.view,
repo.investigate}`, a TTL within `agents.MAX_TTL`. IAM stores only the
subject's SHA-256, never the subject itself, and every bind and unbind is one
`iam.agent.bound` / `iam.agent.unbound` envelope event (IDs, enum codes,
counts), in the same unit of work as the change.

A binding is not authority on its own. At request time (`bound_access`) the
agent's grants are recomputed from the delegator's *current* membership and
grants under the delegator's *verified* code-host standing, intersected with
the binding's scope, narrowed by the verified token, and only then checked by
`authorize_agent`. Demoting the admin, removing the membership, a lost
code-host standing, revocation, or expiry each end the agent's reach without
touching the binding. Agents can never approve, configure, or grant: the
scope cap makes those grants unreachable, and nothing here writes grants.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from flakepilot.identity.application.agent_tokens import narrow
from flakepilot.identity.application.agents import (
    _AGENT_SCOPES,
    AgentAccess,
    AgentRefused,
    authorize_agent,
    check_delegation,
)
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Decision,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
    decide,
)
from flakepilot.identity.application.iam import digest, uuid_ok
from flakepilot.identity.domain.access import effective_grants
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject

__all__ = [
    "AgentBinding",
    "AgentBindingStore",
    "bind_agent",
    "bound_access",
    "subject_digest",
    "unbind_agent",
]

# Auth0 `sub` values are printable ASCII without spaces (`<id>@clients`,
# `auth0|<id>`); the verifier caps them at 255 characters too.
_SUBJECT = re.compile(r"[\x21-\x7e]{1,255}")


@dataclass(frozen=True)
class AgentBinding:
    """An active binding as IAM stored it. The subject is not part of it."""

    binding_id: str
    org_id: str
    repo_id: str
    delegator_id: str
    agent_id: str
    grants: frozenset[Grant]
    expires_at: datetime


class AgentBindingStore(Protocol):
    """IAM persistence for bindings inside one unit of work. Times come from
    PostgreSQL, so expiry agrees across processes."""

    async def append_event(self, event: Event) -> None: ...

    async def org_access(self, *, user_id: str, org_id: str) -> OrgAccess | None: ...

    async def create_agent_binding(
        self,
        *,
        binding_id: str,
        org_id: str,
        repo_id: str,
        delegator_id: str,
        agent_id: str,
        grants: frozenset[Grant],
        subject_hash: bytes,
        ttl: timedelta,
    ) -> datetime | None:
        """The binding's expiry, or None (nothing written) while an unrevoked
        binding for this (org, subject) exists, expired or not."""
        ...

    async def revoke_agent_binding(
        self, *, org_id: str, subject_hash: bytes
    ) -> str | None:
        """The revoked binding's ID, or None when nothing was unrevoked."""
        ...

    async def active_agent_binding(
        self, *, org_id: str, subject_hash: bytes
    ) -> AgentBinding | None:
        """The unrevoked, unexpired binding for this (org, subject), if any."""
        ...


def subject_digest(subject: str) -> bytes | None:
    """The stored form of a token subject; None for a value no token carries."""
    if not _SUBJECT.fullmatch(subject):
        return None
    return digest(subject)


def _admin(admin: OrgAccess, org_id: str) -> None:
    if not uuid_ok(org_id) or admin.org_id != org_id:
        raise ResourceNotFound
    if not admin.org_admin:
        raise PermissionDenied


async def bind_agent(
    store: AgentBindingStore,
    *,
    admin: OrgAccess,
    org_id: str,
    repo_id: str,
    subject: str,
    agent_id: str,
    scopes: frozenset[Grant] | set[Grant],
    ttl: timedelta,
    origin: Origin,
) -> tuple[str, datetime]:
    """Bind one Auth0 subject to a delegation from one org admin, in one unit
    of work. Returns (binding ID, expiry).

    The operator channel has no per-user code-host evidence, so the delegation
    rules are checked here under the deployment's read standing (never
    presented as verified access; see `administration.session_access_overview`).
    That is safe because `bound_access` re-derives the agent's grants from the
    delegator's verified standing on every request. Refusals: `PermissionDenied`
    (not an org admin), `AgentRefused` (`subject_refused`, `already_bound`, or
    any `check_delegation` code); nothing is written.
    """
    _admin(admin, org_id)
    if not uuid_ok(repo_id):
        raise ResourceNotFound
    subject_hash = subject_digest(subject)
    if subject_hash is None:
        raise AgentRefused("subject_refused")
    requested = check_delegation(
        admin,
        org_id=org_id,
        repo_id=repo_id,
        code_host=CodeHostAccess.READ,
        scopes=scopes,
        agent_id=agent_id,
        ttl=ttl,
    )
    binding_id = str(uuid.uuid4())
    expires_at = await store.create_agent_binding(
        binding_id=binding_id,
        org_id=org_id,
        repo_id=repo_id,
        delegator_id=admin.principal_id,
        agent_id=agent_id,
        grants=requested,
        subject_hash=subject_hash,
        ttl=ttl,
    )
    if expires_at is None:
        raise AgentRefused("already_bound")
    await store.append_event(
        Event(
            name="iam.agent.bound",
            actor=Actor(ActorType.USER, admin.principal_id),
            origin=origin,
            subject=Subject("agent_binding", binding_id),
            org_id=org_id,
            repo_id=repo_id,
            after="bound",
            payload={
                "agent_id": agent_id,
                "delegator_id": admin.principal_id,
                "grants": len(requested),
                "ttl_seconds": int(ttl.total_seconds()),
                "expires_at": expires_at.isoformat(),
            },
        )
    )
    return binding_id, expires_at


async def unbind_agent(
    store: AgentBindingStore,
    *,
    admin: OrgAccess,
    org_id: str,
    subject: str,
    origin: Origin,
) -> str:
    """Revoke the subject's binding in this org: "revoked", or "not_bound"
    (a repeat is not an error). Any org admin may revoke, not only the
    delegator."""
    _admin(admin, org_id)
    subject_hash = subject_digest(subject)
    if subject_hash is None:
        raise AgentRefused("subject_refused")
    binding_id = await store.revoke_agent_binding(
        org_id=org_id, subject_hash=subject_hash
    )
    if binding_id is None:
        return "not_bound"
    await store.append_event(
        Event(
            name="iam.agent.unbound",
            actor=Actor(ActorType.USER, admin.principal_id),
            origin=origin,
            subject=Subject("agent_binding", binding_id),
            org_id=org_id,
            before="bound",
            after="revoked",
        )
    )
    return "revoked"


def bound_access(
    binding: AgentBinding,
    delegator: OrgAccess,
    *,
    repo_id: str,
    code_host: CodeHostAccess,
    token_scopes: frozenset[str],
    token_expires_at: datetime,
    now: datetime,
) -> AgentAccess:
    """The agent's access for one request on `repo_id` (the run's repository).

    The delegation is the binding's scope intersected with the delegator's
    current effective grants under `code_host` (the delegator's verified
    standing). Visibility is decided before the token is consulted, so a run
    the agent cannot see is `ResourceNotFound` whatever the token says; then
    `narrow` (the token only removes) and `authorize_agent` (TTL, then
    `repo.investigate`): a visible repository without the grant is
    `PermissionDenied`, an expired delegation `AgentExpired`.
    """
    if delegator.org_id != binding.org_id or delegator.principal_id != (
        binding.delegator_id
    ):
        raise ResourceNotFound
    held = (
        binding.grants
        & effective_grants(delegator, binding.repo_id, code_host)
        & _AGENT_SCOPES
    )
    delegation = AgentAccess(
        principal_id=delegator.principal_id,  # attribution: the delegating admin
        org_id=binding.org_id,
        org_admin=False,
        grants=frozenset((binding.repo_id, grant) for grant in held),
        agent_id=binding.agent_id,
        repo_id=binding.repo_id,
        expires_at=binding.expires_at,
    )
    visible = decide(
        delegation,
        Grant.VIEW,
        org_id=binding.org_id,
        repo_id=repo_id,
        code_host=code_host,
    )
    if visible is Decision.NOT_FOUND:
        raise ResourceNotFound
    access = narrow(
        delegation, scopes=token_scopes, expires_at=token_expires_at, now=now
    )
    authorize_agent(
        access,
        Grant.INVESTIGATE,
        org_id=binding.org_id,
        repo_id=repo_id,
        code_host=code_host,
        now=now,
    )
    return access
