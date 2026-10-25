"""Assist policy, citation counts, and the decision not to call a disabled port."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest

from flakepilot.diagnosis.application.assist import (
    Assist,
    AssistRejected,
    AssistResult,
    UntrustedPart,
)
from flakepilot.diagnosis.application.store import (
    IN_FLIGHT,
    AiPolicy,
    AiRequestRecord,
    DiagStore,
)
from flakepilot.diagnosis.domain.output import DataPolicy, DiagnosisOutcome
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.integrations.application.ports import (
    AiCapabilities,
    AiRequest,
    AiResponse,
)
from flakepilot.integrations.infrastructure.ai import FakeAiAssist, NullAiAssist
from flakepilot.shared_kernel.events import Channel, Event, Origin

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "dddddddd-dddd-dddd-dddd-dddddddddddd"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
EVIDENCE = "11111111-1111-1111-1111-111111111111"
ORIGIN = Origin(Channel.HTTP, "assist-test")
READ = CodeHostAccess.READ


class Memory:
    def __init__(self) -> None:
        self.policies: dict[str, AiPolicy] = {}
        self.requests: list[AiRequestRecord] = []
        self.events: list[Event] = []
        self.fail = False

    async def policy(self, org_id: str) -> AiPolicy:
        return self.policies.get(org_id, AiPolicy(DataPolicy.DISABLED, False))

    async def set_policy(self, org_id: str, policy: AiPolicy) -> None:
        self.policies[org_id] = policy

    async def record_request(self, request: AiRequestRecord) -> None:
        self.requests.append(request)

    async def sent_request_count_today(self, *, org_id: str, day_start: object) -> int:
        return sum(1 for row in self.requests if row.org_id == org_id)

    async def finish_request(self, request: AiRequestRecord) -> None:
        [index] = [
            i
            for i, row in enumerate(self.requests)
            if (row.id, row.org_id, row.validation_reason)
            == (request.id, request.org_id, IN_FLIGHT)
        ]
        self.requests[index] = request

    async def append_event(self, event: Event) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("outbox")


def unit(store: Memory) -> Callable[[], AbstractAsyncContextManager[DiagStore]]:
    @asynccontextmanager
    async def uow() -> AsyncIterator[DiagStore]:
        policies, requests, events = (
            dict(store.policies),
            list(store.requests),
            list(store.events),
        )
        try:
            yield store
        except Exception:
            store.policies.clear()
            store.policies.update(policies)
            store.requests[:] = requests
            store.events[:] = events
            raise

    return uow


class Counting:
    def __init__(self, inner: FakeAiAssist | NullAiAssist) -> None:
        self.inner = inner
        self.calls = 0

    def describe(self) -> AiCapabilities:
        return self.inner.describe()

    async def submit(self, request: AiRequest) -> AiResponse:
        self.calls += 1
        return await self.inner.submit(request)


def assist(store: Memory, port: FakeAiAssist | NullAiAssist | Counting) -> Assist:
    return Assist(unit(store), port)


def admin() -> OrgAccess:
    return OrgAccess(USER, ORG, org_admin=True)


def run_assist(
    service: Assist,
    access: OrgAccess,
    *,
    instructions: str = "summarize",
    parts: tuple[UntrustedPart, ...] = (UntrustedPart("log", "fail"),),
    evidence_ids: frozenset[str] = frozenset({EVIDENCE}),
    code_host: CodeHostAccess = READ,
    org_id: str = ORG,
) -> AssistResult:
    return asyncio.run(
        service.assist(
            access,
            org_id=org_id,
            repo_id=REPO,
            code_host=code_host,
            instructions=instructions,
            parts=parts,
            evidence_ids=evidence_ids,
            origin=ORIGIN,
        )
    )


def test_disabled_policy_does_not_call_the_port(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, port = Memory(), Counting(FakeAiAssist())
    caplog.set_level(logging.INFO, logger="flakepilot.diagnosis")
    result = run_assist(
        assist(store, port),
        admin(),
        instructions="secret-instructions",
        parts=(UntrustedPart("log", "secret-log"),),
    )
    assert port.calls == 0
    assert result.outcome is DiagnosisOutcome.UNAVAILABLE
    assert result.reason == "policy_disabled"
    assert result.linked == result.unverified == result.contradicted == 0
    assert store.requests[0].provider == "none"
    assert store.requests[0].raw_output is None
    assert [event.name for event in store.events] == ["diag.diagnosis.recorded"]
    rendered = repr(store.events) + caplog.text
    assert "secret-instructions" not in rendered
    assert "secret-log" not in rendered
    assert "quota.denied" not in rendered


def test_enabled_fake_links_only_known_ids_and_omits_raw_by_default() -> None:
    store = Memory()
    service = assist(store, FakeAiAssist())
    asyncio.run(
        service.set_policy(
            admin(),
            org_id=ORG,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=ORIGIN,
        )
    )
    cited = (
        '{"hypotheses":[{"summary":"clock skew","cause":"unknown","citations":['
        f'{{"evidence_id":"{EVIDENCE}"}},{{"evidence_id":"not-a-uuid"}}]}}]}}'
    )
    result = run_assist(
        assist(store, FakeAiAssist(scripts={"summarize": cited})),
        OrgAccess(USER, ORG, grants=frozenset({(REPO, Grant.INVESTIGATE)})),
    )
    assert result.outcome is DiagnosisOutcome.PRODUCED
    assert result.reason is None
    assert (result.linked, result.unverified, result.contradicted) == (1, 1, 0)
    assert store.requests[-1].raw_output is None
    assert [event.name for event in store.events] == [
        "diag.ai_policy.updated",
        "diag.ai_request.sent",
        "diag.diagnosis.recorded",
    ]


def test_retain_raw_stores_text_and_a_malformed_script_is_rejected() -> None:
    store = Memory()
    service = assist(store, FakeAiAssist(scripts={"broken": "not-json"}))
    asyncio.run(
        service.set_policy(
            admin(),
            org_id=ORG,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, True),
            origin=ORIGIN,
        )
    )
    result = run_assist(service, admin(), instructions="broken")
    assert result.outcome is DiagnosisOutcome.REJECTED
    assert result.reason == "malformed_json"
    assert store.requests[-1].raw_output == "not-json"
    assert result.contradicted == 0


def test_oversized_output_is_not_stored_even_when_opted_in() -> None:
    store = Memory()
    service = assist(store, FakeAiAssist(scripts={"huge": "x" * 70_000}))
    asyncio.run(
        service.set_policy(
            admin(),
            org_id=ORG,
            policy=AiPolicy(DataPolicy.EVIDENCE_AND_SOURCE, True),
            origin=ORIGIN,
        )
    )
    result = run_assist(service, admin(), instructions="huge")
    assert result.reason == "output_too_large"
    assert store.requests[-1].raw_output is None


def test_null_provider_is_unavailable_when_policy_is_enabled() -> None:
    store, port = Memory(), Counting(NullAiAssist())
    service = assist(store, port)
    asyncio.run(
        service.set_policy(
            admin(),
            org_id=ORG,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=ORIGIN,
        )
    )
    result = run_assist(service, admin())
    assert port.calls == 1
    assert result.outcome is DiagnosisOutcome.UNAVAILABLE
    assert result.reason == "no_provider"
    assert [
        event.name for event in store.events if event.name.startswith("diag.ai_request")
    ] == ["diag.ai_request.sent"]


def test_org_admin_sets_policy_and_can_assist_without_an_explicit_grant() -> None:
    store = Memory()
    service = assist(store, FakeAiAssist(scripts={"summarize": '{"hypotheses":[]}'}))
    asyncio.run(
        service.set_policy(
            admin(),
            org_id=ORG,
            policy=AiPolicy(DataPolicy.DISABLED, True),
            origin=ORIGIN,
        )
    )
    event = store.events[0]
    assert event.name == "diag.ai_policy.updated"
    assert event.before == "disabled" and event.after == "disabled"
    assert event.payload["retain_raw"] is True
    result = run_assist(service, OrgAccess(USER, ORG, org_admin=True))
    assert result.reason == "policy_disabled"
    assert store.requests[-1].retain_raw is True
    assert store.requests[-1].raw_output is None


@pytest.mark.parametrize(
    ("access", "code_host", "org_id", "error"),
    [
        (
            OrgAccess(USER, ORG, grants=frozenset({(REPO, Grant.VIEW)})),
            READ,
            ORG,
            PermissionDenied,
        ),
        (OrgAccess(USER, ORG), READ, ORG, ResourceNotFound),
        (
            OrgAccess(USER, ORG, grants=frozenset({(REPO, Grant.INVESTIGATE)})),
            CodeHostAccess.NONE,
            ORG,
            ResourceNotFound,
        ),
        (admin(), READ, OTHER, ResourceNotFound),
    ],
)
def test_authorization_failures_write_nothing(
    access: OrgAccess,
    code_host: CodeHostAccess,
    org_id: str,
    error: type[Exception],
) -> None:
    store = Memory()
    with pytest.raises(error):
        run_assist(
            assist(store, FakeAiAssist()),
            access,
            code_host=code_host,
            org_id=org_id,
        )
    assert store.requests == [] and store.events == []


def test_non_admin_cannot_set_policy() -> None:
    store = Memory()
    access = OrgAccess(USER, ORG, grants=frozenset({(REPO, Grant.CONFIGURE)}))
    with pytest.raises(PermissionDenied):
        asyncio.run(
            assist(store, NullAiAssist()).set_policy(
                access,
                org_id=ORG,
                policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
                origin=ORIGIN,
            )
        )
    assert store.policies == {}


def test_invalid_input_writes_nothing() -> None:
    store = Memory()
    service = assist(store, FakeAiAssist())
    with pytest.raises(AssistRejected) as too_long:
        run_assist(service, admin(), instructions="i" * 4001)
    with pytest.raises(AssistRejected) as bad_id:
        run_assist(service, admin(), evidence_ids=frozenset({"not-a-uuid"}))
    assert too_long.value.reason == "instructions_too_long"
    assert bad_id.value.reason == "invalid_evidence_id"
    assert store.requests == [] and store.events == []


def test_a_failed_outbox_write_keeps_nothing() -> None:
    store = Memory()
    store.policies[ORG] = AiPolicy(DataPolicy.EVIDENCE_ONLY, False)
    port = Counting(FakeAiAssist())
    store.fail = True
    with pytest.raises(RuntimeError, match="outbox"):
        run_assist(assist(store, port), admin())
    assert store.requests == [] and store.events == []
    assert port.calls == 0  # no durable intent, no call


class Exploding:
    """A port whose call never answers: it raises, or never returns."""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.seen: list[tuple[int, list[str]]] = []
        self.store: Memory | None = None

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider="hosted_test")

    async def submit(self, request: AiRequest) -> AiResponse:
        assert self.store is not None
        # What is durable at the moment the bytes would leave the process.
        self.seen.append(
            (len(self.store.requests), [e.name for e in self.store.events])
        )
        if self.error is not None:
            raise self.error
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


def _intent_row_survives(port: Exploding, store: Memory) -> None:
    [row] = store.requests
    assert (row.outcome, row.validation_reason) == (
        DiagnosisOutcome.UNAVAILABLE,
        IN_FLIGHT,
    )
    assert (row.provider, row.model, row.raw_output) == ("hosted_test", None, None)
    assert [event.name for event in store.events] == ["diag.ai_request.sent"]
    assert store.events[0].subject.id == row.id
    assert set(store.events[0].payload) == {
        "policy",
        "retain_raw",
        "provider",
        "size_bytes",
        "content_sha256",
    }
    assert port.seen == [(1, ["diag.ai_request.sent"])]


def test_intent_is_recorded_before_a_provider_call_that_raises() -> None:
    store = Memory()
    store.policies[ORG] = AiPolicy(DataPolicy.EVIDENCE_ONLY, False)
    port = Exploding(ConnectionResetError("peer reset"))
    port.store = store
    with pytest.raises(ConnectionResetError):
        run_assist(Assist(unit(store), port), admin())
    _intent_row_survives(port, store)


def test_intent_is_recorded_before_a_provider_call_that_times_out() -> None:
    store = Memory()
    store.policies[ORG] = AiPolicy(DataPolicy.EVIDENCE_ONLY, False)
    port = Exploding(None)
    port.store = store

    async def bounded() -> None:
        await asyncio.wait_for(
            Assist(unit(store), port).assist(
                admin(),
                org_id=ORG,
                repo_id=REPO,
                code_host=READ,
                instructions="summarize",
                parts=(UntrustedPart("log", "fail"),),
                evidence_ids=frozenset({EVIDENCE}),
                origin=ORIGIN,
            ),
            timeout=0.05,
        )

    with pytest.raises(TimeoutError):
        asyncio.run(bounded())
    _intent_row_survives(port, store)


def test_an_answered_call_finishes_the_same_row() -> None:
    store = Memory()
    store.policies[ORG] = AiPolicy(DataPolicy.EVIDENCE_ONLY, False)
    result = run_assist(
        assist(store, FakeAiAssist(scripts={"summarize": '{"hypotheses":[]}'})),
        admin(),
    )
    [row] = store.requests
    assert row.id == result.request_id
    assert (row.outcome, row.validation_reason) == (
        DiagnosisOutcome.NO_HYPOTHESIS,
        None,
    )
    assert [event.subject.id for event in store.events] == [row.id, row.id]
