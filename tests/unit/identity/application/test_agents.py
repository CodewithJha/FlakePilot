"""Delegated agent principals (goal 9): the contract tests.

Every structural guarantee in agents.py's docstring is asserted here over
fakes: scope subset ≤ delegator, the approval/configure unreachable cap,
TTL bounds and expiry, the attribution event, and the rules through which
an agent cannot self-grant or boost its scope. The delegation never needs
a store beyond `append_event` — that is the design.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from flakepilot.identity.application.agents import (
    AgentAccess,
    AgentExpired,
    AgentPrincipal,
    AgentRefused,
    Delegate,
    authorize_agent,
)
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.shared_kernel.events import ActorType, Channel, Event, Origin

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
NOW = datetime.now(UTC)
ORIGIN = Origin(Channel.CLI, "agent-test")

GRANTS = frozenset({(REPO, Grant.INVESTIGATE)})
GRANTER = OrgAccess(USER, ORG, org_admin=False, grants=GRANTS)


class FakeIamStore:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def append_event(self, event: Event) -> None:
        self.events.append(event)


class FakeUow:
    def __init__(self) -> None:
        self.store = FakeIamStore()

    def __call__(self) -> "FakeUow":
        return self

    async def __aenter__(self) -> FakeIamStore:
        return self.store

    async def __aexit__(self, *args: object) -> None:
        del args


def delegate() -> tuple[Delegate, FakeIamStore]:
    uow = FakeUow()
    return Delegate(uow), uow.store  # type: ignore[arg-type]


def create(
    delegator: OrgAccess = GRANTER,
    *,
    scopes: Any = frozenset({Grant.INVESTIGATE}),
    ttl: timedelta = timedelta(hours=1),
    agent_id: str = "helper",
) -> Any:
    unit, _ = delegate()
    return unit.create(
        delegator,
        org_id=ORG,
        repo_id=REPO,
        code_host=CodeHostAccess.READ,
        scopes=scopes,
        agent_id=agent_id,
        ttl=ttl,
        now=NOW,
        origin=ORIGIN,
    )


def _access(agent: AgentAccess) -> tuple[Grant, ...]:
    return tuple(sorted(grant for _repo, grant in agent.grants))


def test_a_delegation_within_the_delegators_scope_is_minted() -> None:
    agent = asyncio.run(create())
    assert _access(agent) == (Grant.INVESTIGATE,)
    assert agent.principal_id == USER and agent.org_id == ORG
    assert agent.expires_at == NOW + timedelta(hours=1)


def test_delegation_scope_is_capped_at_view_and_investigate() -> None:
    for refused in (Grant.APPROVE, Grant.CONFIGURE):
        with pytest.raises(AgentRefused) as error:
            asyncio.run(create(scopes=frozenset({refused})))
        assert error.value.reason == "scope_refused"


def test_a_view_only_delegator_is_forbidden_at_the_entry_gate() -> None:
    """Delegation requires `repo.investigate` from the delegator; a member
    who cannot investigate is FORBIDDEN, not refused with a scope code —
    the entry gate is authoritative."""
    viewer = OrgAccess(
        USER, ORG, org_admin=False, grants=frozenset({(REPO, Grant.VIEW)})
    )
    with pytest.raises(PermissionDenied):
        asyncio.run(create(delegator=viewer, scopes=frozenset({Grant.VIEW})))


def test_an_outside_or_grantless_delegator_is_invisible() -> None:
    """A principal with no standing is NOT_FOUND (a refusal that reveals
    nothing), never a scope error."""
    nobody = OrgAccess(USER, ORG, org_admin=False, grants=frozenset())
    with pytest.raises(ResourceNotFound):
        asyncio.run(create(delegator=nobody, scopes=frozenset({Grant.VIEW})))


def test_the_ttl_is_capped_and_positive() -> None:
    with pytest.raises(AgentRefused) as long:
        asyncio.run(create(ttl=timedelta(hours=9)))
    assert long.value.reason == "ttl_refused"
    with pytest.raises(AgentRefused):
        asyncio.run(create(ttl=timedelta(0)))


def test_an_empty_or_badly_named_delegation_is_refused() -> None:
    with pytest.raises(AgentRefused) as empty:
        asyncio.run(create(scopes=frozenset(), agent_id="helper-1"))
    assert empty.value.reason in {"empty_scope", "name_refused"}
    with pytest.raises(AgentRefused):
        asyncio.run(create(agent_id="Bad Name"))


def test_every_delegation_is_one_audited_event() -> None:
    unit, store = delegate()
    asyncio.run(
        unit.create(
            GRANTER,
            org_id=ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
            scopes=frozenset({Grant.INVESTIGATE}),
            agent_id="helper",
            ttl=timedelta(hours=2),
            now=NOW,
            origin=ORIGIN,
        )
    )
    assert [event.name for event in store.events] == ["iam.agent.delegated"]
    event = store.events[0]
    assert event.actor.type is ActorType.USER and event.actor.id == USER
    assert event.subject.type == "agent" and uuid.UUID(event.subject.id)
    assert event.payload["agent_id"] == "helper"
    assert event.payload["delegator_id"] == USER
    assert event.payload["ttl_seconds"] == 2 * 3600
    assert str(event.org_id) == ORG


def test_an_expired_delegation_is_refused_at_use_time() -> None:
    agent = asyncio.run(create(ttl=timedelta(hours=1)))
    authorize_agent(
        agent,
        Grant.INVESTIGATE,
        org_id=ORG,
        repo_id=REPO,
        code_host=CodeHostAccess.READ,
        now=NOW + timedelta(minutes=59),
    )
    with pytest.raises(AgentExpired):
        authorize_agent(
            agent,
            Grant.INVESTIGATE,
            org_id=ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
            now=NOW + timedelta(hours=1, seconds=1),
        )


def test_the_agent_cannot_boost_its_own_scope() -> None:
    """A view-scoped agent asking more of the same moment is denied."""
    agent = asyncio.run(create(scopes=frozenset({Grant.VIEW})))
    authorize_agent(
        agent,
        Grant.VIEW,
        org_id=ORG,
        repo_id=REPO,
        code_host=CodeHostAccess.READ,
        now=NOW,
    )
    with pytest.raises(PermissionDenied):
        authorize_agent(
            agent,
            Grant.INVESTIGATE,
            org_id=ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
            now=NOW,
        )


def test_a_bare_principal_without_access_material_is_denied() -> None:
    """The delegation record alone authorizes nothing: access materiel is
    minted once by `Delegate`, and a re-derived principal is refused — the
    same rule that keeps request-shaped input from ever authorizing."""
    principal = AgentPrincipal(
        agent_id="helper",
        delegator_id=USER,
        repo_id=REPO,
        grants=frozenset(_access(asyncio.run(create()))),
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(PermissionDenied):
        authorize_agent(
            principal,
            Grant.INVESTIGATE,
            org_id=ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
            now=NOW,
        )


def test_the_agent_resolves_as_its_delegator_only() -> None:
    """No agent user exists: the access's principal is the delegator, so the
    IAM grant paths (which take a session-authenticated user) have nothing
    to authenticate an agent with. Self-granting is structurally absent."""
    agent = asyncio.run(create())
    assert agent.principal_id == USER
    assert agent.agent_id != agent.principal_id
    # An agent principal is not a user ID any store row would resolve.
    assert agent.agent_id == "helper"


def test_an_agent_bad_name_in_post_init_is_refused() -> None:
    with pytest.raises(AgentRefused):
        AgentAccess(
            principal_id=USER,
            org_id=ORG,
            grants=frozenset({(REPO, Grant.VIEW)}),
            agent_id="UPPER",
            repo_id=REPO,
            expires_at=NOW,
        )
