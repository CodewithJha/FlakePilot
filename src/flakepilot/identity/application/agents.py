"""Delegated agent principals (ADR-017 decision 6; goal 9).

A delegated agent is a *runtime attribute of a verified member's session*,
not a stored identity: it has no IAM row, no issuer token, no session, and
its authority is a TTL-bounded subset of the delegator's already-verified
grants on one repository. The issuer-agnostic design means an external
identity provider (Auth0 or otherwise) is optional and never consulted
here: the delegator's `OrgAccess` is the only trust input, and it comes
from IAM's own records (`Iam.org_access` — session-authenticated).

Structural guarantees (each backed by a contract test):

- **Scope subset ≤ delegator.** `Delegate.create` computes the agent's
  grants as an intersection with the delegator's effective grants; a
  request beyond that is refused before anything exists.
- **Short-lived.** The TTL is capped (`MAX_TTL`); expiry is absolute, and
  `authorize_agent` refuses an expired delegation.
- **Agents never approve or configure.** The delegation scope cap is
  `{view, investigate}` — the analysis workflow's needs — so execution and
  approval authority is unreachable by construction.
- **Agents cannot self-grant.** `AgentAccess` is never minted from a
  session, never resolves through `Iam.org_access` to a stored user of its
  own (its principal is the delegator, audited), and IAM's grant
  administration entries build their granter from session
  authentication only — an agent principal has nothing to authenticate
  with. Nothing here touches the store's user/session tables.
- **Audited.** Every delegation is one `iam.agent.delegated` envelope
  event (IDs, enum codes, counts only), and downstream attribution rides
  the delegator's actor ID with the agent code alongside.
"""

import re
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    authorize,
)
from flakepilot.identity.application.iam import IamStore
from flakepilot.identity.domain.access import effective_grants
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject

UnitOfWork = Callable[[], AbstractAsyncContextManager[IamStore]]

AGENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")

#: Delegations are analysis-shaped and short-lived. Both bounds are
#: decisions, not defaults: approval/configure authority is unreachable,
#: and a delegation cannot outlive the operator shift that issued it.
MAX_TTL = timedelta(hours=8)

_AGENT_SCOPES = frozenset({Grant.VIEW, Grant.INVESTIGATE})


class AgentRefused(Exception):
    """The delegation itself is refused. `reason` is a short code:
    `scope_refused` (a grant agents may never hold), `scope_exceeded`
    (beyond the delegator), `ttl_refused` (too long), `name_refused`, or
    `empty_scope`. Provider or issuer text is never echoed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AgentExpired(Exception):
    """The delegation's TTL has passed. The short code is `expired`."""


@dataclass(frozen=True)
class AgentPrincipal:
    """The delegation record, runtime-only: never stored, never issued."""

    agent_id: str
    delegator_id: str
    repo_id: str
    grants: frozenset[Grant]
    expires_at: datetime

    def __post_init__(self) -> None:
        if not AGENT_NAME.fullmatch(self.agent_id):
            raise AgentRefused("name_refused")


@dataclass(frozen=True)
class AgentAccess(OrgAccess):
    """The delegated access: the delegator's identity, the agent's scope.

    `principal_id` stays the delegator's verified user ID on purpose —
    every downstream event attributes the human. It satisfies the plain
    `authorize` contract, which is how delegated agents run the
    investigation workflow through its own unchanged gates.
    """

    agent_id: str = field(kw_only=True)
    repo_id: str = field(kw_only=True)
    expires_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if not AGENT_NAME.fullmatch(self.agent_id):
            raise AgentRefused("name_refused")


def authorize_agent(
    agent: AgentPrincipal | AgentAccess,
    grant: Grant,
    *,
    org_id: str,
    repo_id: str,
    code_host: CodeHostAccess,
    now: datetime,
) -> None:
    """The agent authorization decision point: TTL first, then the normal
    grant check. Expired means refused, and the refusal carries no detail
    beyond the short code."""
    if now >= agent.expires_at:
        raise AgentExpired
    if isinstance(agent, AgentAccess):
        authorize(agent, grant, org_id=org_id, repo_id=repo_id, code_host=code_host)
        return
    # A bare principal was issued without access material; nothing to honor.
    raise PermissionDenied


def check_delegation(
    delegator: OrgAccess,
    *,
    org_id: str,
    repo_id: str,
    code_host: CodeHostAccess,
    scopes: frozenset[Grant] | set[Grant],
    agent_id: str,
    ttl: timedelta,
) -> frozenset[Grant]:
    """Every delegation rule, before anything exists: the delegator holds
    `repo.investigate` (the highest authority an agent can inherit), the
    scope is a non-empty subset of `{view, investigate}` and of the
    delegator's effective grants, the name is a short code, and the TTL is
    within the cap. Returns the requested grants."""
    authorize(
        delegator,
        Grant.INVESTIGATE,
        org_id=org_id,
        repo_id=repo_id,
        code_host=code_host,
    )
    requested = frozenset(scopes)
    if Grant.APPROVE in requested or Grant.CONFIGURE in requested:
        raise AgentRefused("scope_refused")
    if not requested <= _AGENT_SCOPES:
        raise AgentRefused("scope_refused")
    if not requested:
        raise AgentRefused("empty_scope")
    effective = effective_grants(delegator, repo_id, code_host) & _AGENT_SCOPES
    if not requested <= effective:
        raise AgentRefused("scope_exceeded")
    if not AGENT_NAME.fullmatch(agent_id):
        raise AgentRefused("name_refused")
    if ttl <= timedelta(0) or ttl > MAX_TTL:
        raise AgentRefused("ttl_refused")
    return requested


class Delegate:
    """Mints a delegated agent from a verified delegator — audited once."""

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def create(
        self,
        delegator: OrgAccess,
        *,
        org_id: str,
        repo_id: str,
        code_host: CodeHostAccess,
        scopes: frozenset[Grant] | set[Grant],
        agent_id: str,
        ttl: timedelta,
        now: datetime,
        origin: Origin,
    ) -> AgentAccess:
        """The delegation itself: anchored to one repo, bounded by the
        delegator's grants and the TTL cap (`check_delegation`), and recorded
        in IAM's outbox in the same transaction discipline as every identity
        event."""
        requested = check_delegation(
            delegator,
            org_id=org_id,
            repo_id=repo_id,
            code_host=code_host,
            scopes=scopes,
            agent_id=agent_id,
            ttl=ttl,
        )
        expires_at = now + ttl
        access = AgentAccess(
            principal_id=delegator.principal_id,  # attribution: the human
            org_id=delegator.org_id,
            org_admin=False,
            grants=frozenset((repo_id, grant) for grant in requested),
            agent_id=agent_id,
            repo_id=repo_id,
            expires_at=expires_at,
        )
        event = Event(
            name="iam.agent.delegated",
            actor=Actor(ActorType.USER, delegator.principal_id),
            origin=origin,
            subject=Subject("agent", str(uuid.uuid4())),
            org_id=org_id,
            repo_id=repo_id,
            after="delegated",
            payload={
                "agent_id": agent_id,
                "delegator_id": delegator.principal_id,
                "grants": len(requested),
                "ttl_seconds": int(ttl.total_seconds()),
                "expires_at": expires_at.isoformat(),
            },
        )
        async with self._unit_of_work() as store:
            await store.append_event(event)
        return access
