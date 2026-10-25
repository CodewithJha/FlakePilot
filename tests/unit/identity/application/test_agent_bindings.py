"""Stored Auth0 agent bindings (goal 9): bind, unbind, and per-request access.

`bind_agent` and `unbind_agent` over an in-memory store; `bound_access` as the
pure chain the agent route runs: delegation from the delegator's live grants,
visibility before the token, `narrow`, then `authorize_agent`.
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from flakepilot.identity.application.agent_bindings import (
    AgentBinding,
    bind_agent,
    bound_access,
    subject_digest,
    unbind_agent,
)
from flakepilot.identity.application.agents import (
    MAX_TTL,
    AgentAccess,
    AgentExpired,
    AgentRefused,
)
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.shared_kernel.events import ActorType, Channel, Event, Origin

ORG = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_REPO = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ADMIN = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SUBJECT = "agentclient123@clients"
NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
ORIGIN = Origin(Channel.CLI, "binding-test")
ADMIN_ACCESS = OrgAccess(ADMIN, ORG, org_admin=True)
BOTH = frozenset({Grant.VIEW, Grant.INVESTIGATE})


class MemoryStore:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.rows: dict[tuple[str, bytes], dict[str, object]] = {}

    async def append_event(self, event: Event) -> None:
        self.events.append(event)

    async def org_access(self, *, user_id: str, org_id: str) -> OrgAccess | None:
        return ADMIN_ACCESS if (user_id, org_id) == (ADMIN, ORG) else None

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
        if (org_id, subject_hash) in self.rows:
            return None
        self.rows[(org_id, subject_hash)] = {
            "id": binding_id,
            "repo_id": repo_id,
            "delegator_id": delegator_id,
            "agent_id": agent_id,
            "grants": grants,
        }
        return NOW + ttl

    async def revoke_agent_binding(
        self, *, org_id: str, subject_hash: bytes
    ) -> str | None:
        row = self.rows.pop((org_id, subject_hash), None)
        return None if row is None else str(row["id"])

    async def active_agent_binding(
        self, *, org_id: str, subject_hash: bytes
    ) -> AgentBinding | None:
        raise AssertionError("not used by bind/unbind")


def bind(store: MemoryStore, **overrides: object) -> tuple[str, datetime]:
    arguments: dict[str, object] = {
        "admin": ADMIN_ACCESS,
        "org_id": ORG,
        "repo_id": REPO,
        "subject": SUBJECT,
        "agent_id": "mastra_triage",
        "scopes": BOTH,
        "ttl": timedelta(minutes=30),
        "origin": ORIGIN,
        **overrides,
    }
    return asyncio.run(bind_agent(store, **arguments))  # type: ignore[arg-type]


def refusal(store: MemoryStore, **overrides: object) -> str:
    with pytest.raises(AgentRefused) as raised:
        bind(store, **overrides)
    return raised.value.reason


def test_an_org_admin_binds_a_subject_and_one_event_names_no_subject() -> None:
    store = MemoryStore()
    binding_id, expires_at = bind(store)
    assert expires_at == NOW + timedelta(minutes=30)
    [row] = store.rows.values()
    assert row["delegator_id"] == ADMIN and row["grants"] == BOTH
    assert list(store.rows) == [(ORG, hashlib.sha256(SUBJECT.encode()).digest())]
    [event] = store.events
    assert event.name == "iam.agent.bound"
    assert (event.actor.type, event.actor.id) == (ActorType.USER, ADMIN)
    assert (event.subject.type, event.subject.id) == ("agent_binding", binding_id)
    assert (event.org_id, event.repo_id, event.after) == (ORG, REPO, "bound")
    assert event.payload["agent_id"] == "mastra_triage"
    assert event.payload["delegator_id"] == ADMIN
    assert event.payload["grants"] == 2 and event.payload["ttl_seconds"] == 1800
    assert SUBJECT not in repr(event) and "agentclient123" not in repr(event)


def test_only_an_org_admin_of_this_org_may_bind() -> None:
    store = MemoryStore()
    member = OrgAccess(ADMIN, ORG, grants=frozenset({(REPO, Grant.INVESTIGATE)}))
    with pytest.raises(PermissionDenied):
        bind(store, admin=member)
    with pytest.raises(ResourceNotFound):
        bind(store, admin=OrgAccess(ADMIN, OTHER_REPO, org_admin=True))
    with pytest.raises(ResourceNotFound):
        bind(store, repo_id="not-a-uuid")
    assert store.rows == {} and store.events == []


def test_scope_ttl_name_and_subject_rules_refuse_before_writing() -> None:
    store = MemoryStore()
    assert refusal(store, scopes={Grant.APPROVE}) == "scope_refused"
    assert refusal(store, scopes={Grant.CONFIGURE, Grant.VIEW}) == "scope_refused"
    assert refusal(store, scopes=set()) == "empty_scope"
    assert refusal(store, ttl=MAX_TTL + timedelta(seconds=1)) == "ttl_refused"
    assert refusal(store, ttl=timedelta(0)) == "ttl_refused"
    assert refusal(store, agent_id="Bad Name") == "name_refused"
    assert refusal(store, subject="has space@clients") == "subject_refused"
    assert refusal(store, subject="") == "subject_refused"
    assert refusal(store, subject="x" * 256) == "subject_refused"
    assert store.rows == {} and store.events == []


def test_a_second_binding_for_the_same_subject_is_refused_until_revoked() -> None:
    store = MemoryStore()
    bind(store)
    assert refusal(store, repo_id=OTHER_REPO) == "already_bound"
    assert len(store.events) == 1
    assert asyncio.run(unbind(store)) == "revoked"
    bind(store, repo_id=OTHER_REPO)
    assert [e.name for e in store.events] == [
        "iam.agent.bound",
        "iam.agent.unbound",
        "iam.agent.bound",
    ]


async def unbind(store: MemoryStore, admin: OrgAccess = ADMIN_ACCESS) -> str:
    return await unbind_agent(
        store, admin=admin, org_id=ORG, subject=SUBJECT, origin=ORIGIN
    )


def test_unbinding_is_audited_once_and_a_repeat_is_not_bound() -> None:
    store = MemoryStore()
    binding_id, _ = bind(store)
    assert asyncio.run(unbind(store)) == "revoked"
    assert asyncio.run(unbind(store)) == "not_bound"
    unbound = store.events[-1]
    assert unbound.name == "iam.agent.unbound"
    assert (unbound.subject.id, unbound.before, unbound.after) == (
        binding_id,
        "bound",
        "revoked",
    )
    assert len(store.events) == 2
    with pytest.raises(PermissionDenied):
        asyncio.run(unbind(store, OrgAccess(ADMIN, ORG)))


def test_the_subject_digest_refuses_values_no_token_carries() -> None:
    assert subject_digest(SUBJECT) == hashlib.sha256(SUBJECT.encode()).digest()
    assert subject_digest("a b") is None and subject_digest("\x00") is None


# bound_access: the per-request chain.

BINDING = AgentBinding(
    binding_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    org_id=ORG,
    repo_id=REPO,
    delegator_id=ADMIN,
    agent_id="mastra_triage",
    grants=BOTH,
    expires_at=NOW + timedelta(hours=1),
)


def access(
    *,
    binding: AgentBinding = BINDING,
    delegator: OrgAccess = ADMIN_ACCESS,
    repo_id: str = REPO,
    code_host: CodeHostAccess = CodeHostAccess.READ,
    scopes: frozenset[str] = frozenset({"repo.view", "repo.investigate"}),
    token_expires_at: datetime = NOW + timedelta(minutes=10),
    now: datetime = NOW,
) -> AgentAccess:
    return bound_access(
        binding,
        delegator,
        repo_id=repo_id,
        code_host=code_host,
        token_scopes=scopes,
        token_expires_at=token_expires_at,
        now=now,
    )


def test_the_agent_holds_the_binding_narrowed_and_attributes_the_admin() -> None:
    granted = access()
    assert granted.principal_id == ADMIN and not granted.org_admin
    assert granted.agent_id == "mastra_triage" and granted.repo_id == REPO
    assert granted.grants == {(REPO, Grant.VIEW), (REPO, Grant.INVESTIGATE)}
    assert granted.expires_at == NOW + timedelta(minutes=10)  # the earlier one


def test_an_invisible_run_is_not_found_whatever_the_token_says() -> None:
    for scopes in (frozenset({"repo.investigate"}), frozenset({"other"})):
        with pytest.raises(ResourceNotFound):
            access(repo_id=OTHER_REPO, scopes=scopes)
        # An unverified or absent delegator standing hides the run too.
        with pytest.raises(ResourceNotFound):
            access(code_host=CodeHostAccess.NONE, scopes=scopes)


def test_the_delegators_live_grants_bound_the_agent() -> None:
    demoted = OrgAccess(ADMIN, ORG, org_admin=False)
    with pytest.raises(ResourceNotFound):
        access(delegator=demoted)
    viewer = OrgAccess(ADMIN, ORG, grants=frozenset({(REPO, Grant.VIEW)}))
    with pytest.raises(PermissionDenied):
        access(delegator=viewer)
    with pytest.raises(ResourceNotFound):
        access(delegator=OrgAccess("someone-else", ORG, org_admin=True))


def test_a_token_without_investigate_is_forbidden_on_a_visible_run() -> None:
    with pytest.raises(PermissionDenied):
        access(scopes=frozenset({"repo.view"}))
    with pytest.raises(PermissionDenied):
        access(scopes=frozenset({"repo.approve", "repo.configure"}))
    view_binding = AgentBinding(
        **{**BINDING.__dict__, "grants": frozenset({Grant.VIEW})}
    )
    with pytest.raises(PermissionDenied):
        access(binding=view_binding)


def test_expiry_of_either_side_refuses() -> None:
    with pytest.raises(AgentExpired):
        access(now=BINDING.expires_at)
    with pytest.raises(AgentExpired):
        access(token_expires_at=NOW)
