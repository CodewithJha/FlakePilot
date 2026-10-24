"""Diagnosis policy, request metadata, and outbox against PostgreSQL."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import exc, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.diagnosis.application.assist import Assist, UntrustedPart
from flakepilot.diagnosis.application.store import (
    IN_FLIGHT,
    AiPolicy,
    AiRequestRecord,
    DiagStore,
)
from flakepilot.diagnosis.domain.output import DataPolicy, DiagnosisOutcome
from flakepilot.diagnosis.infrastructure.store import SqlDiagStore
from flakepilot.identity.application.authorization import CodeHostAccess, OrgAccess
from flakepilot.integrations.application.ports import (
    AiCapabilities,
    AiRequest,
    AiResponse,
)
from flakepilot.integrations.infrastructure.ai import FakeAiAssist
from flakepilot.interface.composition_root.database import create_engine, transaction
from flakepilot.shared_kernel.events import (
    Actor,
    ActorType,
    Channel,
    Event,
    Origin,
    Subject,
)

pytestmark = pytest.mark.integration

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ACTOR = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def run[T](url: str, scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def main() -> T:
        engine = create_engine(url)
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def sql(engine: AsyncEngine, statement: str, **params: object) -> list[Any]:
    async with transaction(engine) as session:
        result = await session.execute(text(statement), params)
        returns_rows = isinstance(result, CursorResult) and result.returns_rows
        return list(result) if returns_rows else []


def _request(
    *,
    retain_raw: bool = False,
    raw_output: str | None = None,
    outcome: DiagnosisOutcome = DiagnosisOutcome.PRODUCED,
    validation_reason: str | None = None,
) -> AiRequestRecord:
    return AiRequestRecord(
        id=str(uuid.uuid4()),
        org_id=ORG,
        repo_id=REPO,
        actor_id=ACTOR,
        policy_level=DataPolicy.EVIDENCE_ONLY,
        retain_raw=retain_raw,
        provider="fake",
        model="deterministic",
        payload_sha256=b"a" * 32,
        payload_size=12,
        outcome=outcome,
        validation_reason=validation_reason,
        linked_count=1,
        unverified_count=0,
        contradicted_count=0,
        raw_output=raw_output,
    )


def _event() -> Event:
    return Event(
        name="diag.diagnosis.recorded",
        actor=Actor(ActorType.USER, ACTOR),
        origin=Origin(Channel.HTTP, "diagnosis-test"),
        subject=Subject("ai_request", str(uuid.uuid4())),
        org_id=ORG,
        repo_id=REPO,
        payload={"retain_raw": False, "linked": 1},
    )


def test_migration_creates_the_diag_schema(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> list[Any]:
        return await sql(
            engine,
            "SELECT to_regclass('diag.ai_policies'), to_regclass('diag.ai_requests'), "
            "to_regclass('diag.outbox')",
        )

    assert run(url, scenario) == [
        ("diag.ai_policies", "diag.ai_requests", "diag.outbox")
    ]


def test_missing_policy_is_disabled_and_writes_nothing(url: str) -> None:
    org_id = str(uuid.uuid4())

    async def scenario(engine: AsyncEngine) -> tuple[AiPolicy, int]:
        async with transaction(engine) as session:
            found = await SqlDiagStore(session).policy(org_id)
        [count] = await sql(
            engine,
            "SELECT count(*) FROM diag.ai_policies WHERE org_id = :org",
            org=org_id,
        )
        return found, count[0]

    assert run(url, scenario) == (AiPolicy(DataPolicy.DISABLED, False), 0)


def test_raw_output_requires_the_opt_in(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[int, int]:
        async def write(request: AiRequestRecord) -> None:
            async with transaction(engine) as session:
                await SqlDiagStore(session).record_request(request)

        with pytest.raises(exc.IntegrityError):
            await write(_request(raw_output="model text"))
        await write(_request(retain_raw=True, raw_output="model text"))
        [rejected] = await sql(
            engine, "SELECT count(*) FROM diag.ai_requests WHERE raw_output IS NOT NULL"
        )
        [kept] = await sql(engine, "SELECT count(*) FROM diag.ai_requests")
        return rejected[0], kept[0]

    assert run(url, scenario) == (1, 1)


def test_rejected_outcome_needs_a_reason(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        async with transaction(engine) as session:
            await SqlDiagStore(session).record_request(
                _request(outcome=DiagnosisOutcome.REJECTED, validation_reason=None)
            )

    with pytest.raises(exc.IntegrityError):
        run(url, scenario)


class _FailingOutbox(SqlDiagStore):
    async def append_event(self, event: Event) -> None:
        await super().append_event(event)
        await super().append_event(event)


def test_a_failed_outbox_write_rolls_back_the_change(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[tuple[int, ...], tuple[int, ...]]:
        async def counts() -> tuple[int, ...]:
            [row] = await sql(
                engine,
                "SELECT (SELECT count(*) FROM diag.ai_policies), "
                "(SELECT count(*) FROM diag.ai_requests), "
                "(SELECT count(*) FROM diag.outbox)",
            )
            return tuple(row)

        async def attempt() -> None:
            async with transaction(engine) as session:
                store: DiagStore = _FailingOutbox(session)
                await store.set_policy(ORG, AiPolicy(DataPolicy.EVIDENCE_ONLY, True))
                await store.record_request(_request(retain_raw=True, raw_output="kept"))
                await store.append_event(_event())

        before = await counts()
        with pytest.raises(exc.IntegrityError):
            await attempt()
        return before, await counts()

    before, after = run(url, scenario)
    assert after == before


def test_a_successful_change_keeps_one_unpublished_event(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        async with transaction(engine) as session:
            store = SqlDiagStore(session)
            await store.set_policy(ORG, AiPolicy(DataPolicy.EVIDENCE_ONLY, False))
            await store.record_request(_request())
            await store.append_event(_event())
        [policy] = await sql(
            engine,
            "SELECT level, retain_raw FROM diag.ai_policies WHERE org_id = :org",
            org=ORG,
        )
        [event] = await sql(
            engine,
            "SELECT name, published_at IS NULL FROM diag.outbox WHERE org_id = :org",
            org=ORG,
        )
        return (*policy, *event)

    assert run(url, scenario) == (
        "evidence_only",
        False,
        "diag.diagnosis.recorded",
        True,
    )


def test_assist_commits_metadata_and_rolls_back_with_the_outbox(url: str) -> None:
    org_id, repo_id, actor = (str(uuid.uuid4()) for _ in range(3))
    access = OrgAccess(actor, org_id, org_admin=True)
    origin = Origin(Channel.HTTP, "assist-commit")

    def bound(
        engine: AsyncEngine, kind: type[SqlDiagStore]
    ) -> Callable[[], AbstractAsyncContextManager[DiagStore]]:
        @asynccontextmanager
        async def unit_of_work() -> AsyncIterator[DiagStore]:
            async with transaction(engine) as session:
                yield kind(session)

        return unit_of_work

    async def scenario(engine: AsyncEngine) -> tuple[object, ...]:
        service = Assist(bound(engine, SqlDiagStore), FakeAiAssist())
        await service.set_policy(
            access,
            org_id=org_id,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=origin,
        )
        result = await service.assist(
            access,
            org_id=org_id,
            repo_id=repo_id,
            code_host=CodeHostAccess.READ,
            instructions="summarize",
            parts=(UntrustedPart("log", "fail"),),
            evidence_ids=frozenset(),
            origin=origin,
        )
        failing = Assist(bound(engine, _FailingOutbox), FakeAiAssist())
        with pytest.raises(exc.IntegrityError):
            await failing.assist(
                access,
                org_id=org_id,
                repo_id=repo_id,
                code_host=CodeHostAccess.READ,
                instructions="summarize",
                parts=(UntrustedPart("log", "fail"),),
                evidence_ids=frozenset(),
                origin=Origin(Channel.HTTP, "assist-rollback"),
            )
        [requests] = await sql(
            engine,
            "SELECT count(*), count(*) FILTER (WHERE raw_output IS NULL) "
            "FROM diag.ai_requests WHERE org_id = :org",
            org=org_id,
        )
        names = await sql(
            engine,
            "SELECT name FROM diag.outbox WHERE org_id = :org ORDER BY seq",
            org=org_id,
        )
        [rolled_back] = await sql(
            engine,
            "SELECT count(*) FROM diag.outbox WHERE correlation_id = 'assist-rollback'",
        )
        return (
            result.outcome,
            result.reason,
            result.unverified,
            result.contradicted,
            requests[0],
            requests[1],
            rolled_back[0],
            tuple(row[0] for row in names),
        )

    assert run(url, scenario) == (
        DiagnosisOutcome.PRODUCED,
        None,
        1,
        0,
        1,
        1,
        0,
        (
            "diag.ai_policy.updated",
            "diag.ai_request.sent",
            "diag.diagnosis.recorded",
        ),
    )


class _Unreachable:
    """A network-shaped port whose call fails after the bytes would have left."""

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider="hosted_test")

    async def submit(self, request: AiRequest) -> AiResponse:
        raise TimeoutError("socket timeout")


def test_assist_intent_is_committed_before_the_provider_call(url: str) -> None:
    org_id, repo_id, actor = (str(uuid.uuid4()) for _ in range(3))
    access = OrgAccess(actor, org_id, org_admin=True)

    @asynccontextmanager
    async def unit_of_work(engine: AsyncEngine) -> AsyncIterator[DiagStore]:
        async with transaction(engine) as session:
            yield SqlDiagStore(session)

    async def scenario(engine: AsyncEngine) -> tuple[list[Any], list[Any]]:
        service = Assist(lambda: unit_of_work(engine), _Unreachable())
        await service.set_policy(
            access,
            org_id=org_id,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=Origin(Channel.HTTP, "assist-intent"),
        )
        with pytest.raises(TimeoutError):
            await service.assist(
                access,
                org_id=org_id,
                repo_id=repo_id,
                code_host=CodeHostAccess.READ,
                instructions="summarize",
                parts=(UntrustedPart("log", "fail"),),
                evidence_ids=frozenset(),
                origin=Origin(Channel.HTTP, "assist-intent"),
            )
        rows = await sql(
            engine,
            "SELECT id, provider, outcome, validation_reason, model "
            "FROM diag.ai_requests WHERE org_id = :org",
            org=org_id,
        )
        events = await sql(
            engine,
            "SELECT name, subject_id FROM diag.outbox WHERE org_id = :org ORDER BY seq",
            org=org_id,
        )
        return rows, events

    rows, events = run(url, scenario)
    [(request_id, provider, outcome, reason, model)] = rows
    assert (provider, outcome, reason, model) == (
        "hosted_test",
        "unavailable",
        IN_FLIGHT,
        None,
    )
    assert [(name, str(subject)) for name, subject in events] == [
        ("diag.ai_policy.updated", org_id),
        ("diag.ai_request.sent", str(request_id)),
    ]


def test_finish_request_never_rewrites_a_finished_row(url: str) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        finished = _request()
        async with transaction(engine) as session:
            await SqlDiagStore(session).record_request(finished)
        async with transaction(engine) as session:
            await SqlDiagStore(session).finish_request(finished)

    with pytest.raises(exc.NoResultFound):
        run(url, scenario)


@pytest.fixture
def url(migrated_database_url: str) -> str:
    return migrated_database_url
