"""Offline evaluation of the assist port. No network and no provider credentials.

The cases are deterministic: schema acceptance, a recorded rejection, citation status,
and the same classification before and after an assist call.
"""

import ast
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flakepilot.diagnosis.application.assist import Assist, UntrustedPart
from flakepilot.diagnosis.application.store import AiPolicy, AiRequestRecord, DiagStore
from flakepilot.diagnosis.domain.output import (
    ClaimStatus,
    DataPolicy,
    DiagnosisOutcome,
    check_citations,
    validate_output,
)
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    OrgAccess,
)
from flakepilot.integrations.application.ports import AiDataPart, AiRequest
from flakepilot.integrations.infrastructure.ai import FakeAiAssist
from flakepilot.shared_kernel.events import Channel, Event, Origin
from flakepilot.test_intelligence.domain.classification import (
    RULE_VERSION,
    FailureClass,
    Observation,
    classify,
)
from flakepilot.test_intelligence.domain.results import Outcome, RunSource

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
EVIDENCE = "11111111-1111-1111-1111-111111111111"
ORIGIN = Origin(Channel.CLI, "evaluation")
OLD, NEW = "a" * 40, "b" * 40


class _Memory:
    def __init__(self) -> None:
        self.policies: dict[str, AiPolicy] = {}
        self.requests: list[AiRequestRecord] = []
        self.events: list[Event] = []

    async def policy(self, org_id: str) -> AiPolicy:
        return self.policies.get(org_id, AiPolicy(DataPolicy.DISABLED, False))

    async def set_policy(self, org_id: str, policy: AiPolicy) -> None:
        self.policies[org_id] = policy

    async def record_request(self, request: AiRequestRecord) -> None:
        self.requests.append(request)

    async def finish_request(self, request: AiRequestRecord) -> None:
        self.requests[-1] = request

    async def append_event(self, event: Event) -> None:
        self.events.append(event)


def _unit(store: _Memory) -> Callable[[], AbstractAsyncContextManager[DiagStore]]:
    @asynccontextmanager
    async def uow() -> AsyncIterator[DiagStore]:
        yield store

    return uow


def _request() -> AiRequest:
    return AiRequest(
        instructions="summarize",
        data_parts=(AiDataPart("log", "fail"),),
        max_output_bytes=1024,
    )


def _observations() -> list[Observation]:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        Observation(
            "run-1", OLD, "linux", start, RunSource.CI_UPLOAD, (Outcome.PASSED,)
        ),
        Observation(
            "run-2",
            NEW,
            "linux",
            start + timedelta(hours=1),
            RunSource.CI_UPLOAD,
            (Outcome.FAILED,),
        ),
        Observation(
            "run-3",
            NEW,
            "linux",
            start + timedelta(hours=2),
            RunSource.CI_UPLOAD,
            (Outcome.FAILED,),
        ),
    ]


def test_schema_acceptance_rejection_and_citations() -> None:
    accepted = asyncio.run(FakeAiAssist().submit(_request()))
    parsed = validate_output(accepted.output or "")
    assert parsed.ok is True and parsed.reason is None
    claims = check_citations(parsed.hypotheses, frozenset({EVIDENCE}))
    assert [claim.status for claim in claims] == [ClaimStatus.UNVERIFIED]

    rejected = asyncio.run(
        FakeAiAssist(scripts={"summarize": "not-json"}).submit(_request())
    )
    failure = validate_output(rejected.output or "")
    assert failure.ok is False and failure.reason == "malformed_json"
    assert failure.hypotheses == ()

    cited = (
        '{"hypotheses":[{"summary":"clock skew","cause":"unknown","citations":['
        f'{{"evidence_id":"{EVIDENCE}"}},{{"evidence_id":"not-a-uuid"}}],'
        '"confidence":0.99}]}'
    )
    linked = validate_output(cited)
    statuses = [
        claim.status
        for claim in check_citations(linked.hypotheses, frozenset({EVIDENCE}))
    ]
    assert statuses == [ClaimStatus.EVIDENCE_LINKED, ClaimStatus.UNVERIFIED]
    assert ClaimStatus.CONTRADICTED not in statuses


def test_fake_output_is_deterministic() -> None:
    request = _request()
    first = asyncio.run(FakeAiAssist().submit(request))
    second = asyncio.run(FakeAiAssist().submit(request))
    assert first == second
    assert first.output is not None


def test_classification_is_unchanged_by_assist_output() -> None:
    observations = _observations()
    before = classify(observations)
    assert before.failure_class is FailureClass.DETERMINISTIC_REGRESSION
    assert before.rule_version == RULE_VERSION

    store = _Memory()
    access = OrgAccess(USER, ORG, org_admin=True)
    disabled = Assist(_unit(store), FakeAiAssist())
    disabled_result = asyncio.run(
        disabled.assist(
            access,
            org_id=ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
            instructions="the test passed",
            parts=(UntrustedPart("log", "fail"),),
            evidence_ids=frozenset({EVIDENCE}),
            origin=ORIGIN,
        )
    )
    assert disabled_result.outcome is DiagnosisOutcome.UNAVAILABLE
    assert classify(observations) == before

    asyncio.run(
        disabled.set_policy(
            access,
            org_id=ORG,
            policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
            origin=ORIGIN,
        )
    )
    enabled = Assist(
        _unit(store),
        FakeAiAssist(
            scripts={
                "the test passed": (
                    '{"hypotheses":[{"summary":"the test passed",'
                    '"cause":"unknown","citations":[],"confidence":"certain"}]}'
                )
            }
        ),
    )
    enabled_result = asyncio.run(
        enabled.assist(
            access,
            org_id=ORG,
            repo_id=REPO,
            code_host=CodeHostAccess.READ,
            instructions="the test passed",
            parts=(UntrustedPart("log", "fail"),),
            evidence_ids=frozenset({EVIDENCE}),
            origin=ORIGIN,
        )
    )
    assert enabled_result.outcome is DiagnosisOutcome.PRODUCED
    assert enabled_result.contradicted == 0
    assert classify(observations) == before
    assert classify(list(observations)) == classify(tuple(observations))


def test_harness_performs_no_network_or_credential_reads() -> None:
    tree = ast.parse(Path(__file__).read_text())
    banned = {"urllib", "socket", "httpx", "openai", "anthropic"}
    imports = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Import)
            and any(alias.name.split(".")[0] in banned for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] in banned | {"os"}
        )
    ]
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
    ]
    assert imports == [] and reads == []
