"""Delegated agent principals over real `iam` tables (goal 9).

The delegation is minted from a real `OrgAccess` and lands exactly one
`iam.agent.delegated` row in `iam.outbox` on the migrated schema — the
runtime-only design means there is nothing else to persist, which is the
point: an agent that exists nowhere cannot borrow a standing identity.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.identity.application.agents import Delegate, authorize_agent
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
)
from flakepilot.identity.application.iam import IamStore
from flakepilot.identity.infrastructure.store import SqlIamStore
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.shared_kernel.events import Channel, Origin

pytestmark = pytest.mark.integration

ORIGIN = Origin(Channel.CLI, "agent-delegation-integration")


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def iam_uow(
    engine: AsyncEngine,
) -> Callable[[], AbstractAsyncContextManager[IamStore]]:
    @asynccontextmanager
    async def unit() -> AsyncIterator[SqlIamStore]:
        async with transaction(engine) as session:
            yield SqlIamStore(session)

    return unit


async def sql(engine: AsyncEngine, statement: str, **params: object) -> list[Any]:
    async with transaction(engine) as session:
        result = await session.execute(text(statement), params)
        if isinstance(result, CursorResult) and result.returns_rows:
            return list(result)
        return []


def standing() -> tuple[str, str, str, OrgAccess]:
    org, repo, user = (str(uuid.uuid4()) for _ in range(3))
    access = OrgAccess(
        user,
        org,
        org_admin=False,
        grants=frozenset({(repo, Grant.INVESTIGATE)}),
    )
    return org, repo, user, access


def test_delegation_appends_exactly_one_row_to_the_identity_outbox(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> Any:
        org, repo, _user, delegator = standing()
        delegate = Delegate(iam_uow(engine))
        agent = await delegate.create(
            delegator,
            org_id=org,
            repo_id=repo,
            code_host=CodeHostAccess.READ,
            scopes=frozenset({Grant.INVESTIGATE}),
            agent_id="helper",
            ttl=timedelta(hours=2),
            now=datetime.now(UTC),
            origin=ORIGIN,
        )
        events = await sql(
            engine,
            "SELECT name, actor_type, actor_id, subject_type, subject_id, "
            "after, payload, published_at IS NULL AS pending "
            "FROM iam.outbox WHERE org_id = :org ORDER BY seq",
            org=org,
        )
        users = await sql(engine, "SELECT count(*) AS n FROM iam.users")
        return agent, events, int(users[0].n)

    agent, events, user_count = run(url, scenario)
    rows = [row for row in events if row.name == "iam.agent.delegated"]
    assert len(rows) == 1
    row = rows[0]
    # Attribution: the human delegator is the actor; the agent is the subject.
    assert row.actor_type == "user" and str(row.actor_id) == agent.principal_id
    assert row.subject_type == "agent" and uuid.UUID(str(row.subject_id))
    assert row.after == "delegated"
    assert row.pending is True
    assert row.payload["agent_id"] == "helper"
    # The runtime-only contract: no user row was minted for the agent.
    assert user_count == 0


def test_the_delegated_access_passes_the_plain_authorization_gate(url: str) -> None:
    """The AgentAccess rides the existing gates; no parallel agent path.
    Approval stays unreachable: the agent's scope never contains it."""

    async def scenario(engine: AsyncEngine) -> Any:
        org, repo, _user, delegator = standing()
        delegate = Delegate(iam_uow(engine))
        agent = await delegate.create(
            delegator,
            org_id=org,
            repo_id=repo,
            code_host=CodeHostAccess.READ,
            scopes=frozenset({Grant.INVESTIGATE, Grant.VIEW}),
            agent_id="helper",
            ttl=timedelta(hours=1),
            now=datetime.now(UTC),
            origin=ORIGIN,
        )
        return repo, agent

    repo, agent = run(url, scenario)
    authorize_agent(
        agent,
        Grant.INVESTIGATE,
        org_id=agent.org_id,
        repo_id=repo,
        code_host=CodeHostAccess.READ,
        now=datetime.now(UTC),
    )
    with pytest.raises(PermissionDenied):
        authorize_agent(
            agent,
            Grant.APPROVE,
            org_id=agent.org_id,
            repo_id=repo,
            code_host=CodeHostAccess.READ,
            now=datetime.now(UTC),
        )


@pytest.fixture
def url(migrated_database_url: str) -> str:
    return migrated_database_url
