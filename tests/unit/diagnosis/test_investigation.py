"""The bounded investigation workflow over fakes (goal 2 + goal 15).

Every stage boundary is exercised here with injected fakes: the TI run
viewer, the assist policy gate (the real `Assist` use case over a fake store
and canned ports), the reliability/prioritization use case, and the
diagnostic outbox that receives the provenance event. The integration suite
covers real tables; this file covers the choreography: stage statuses,
budgets, stopping conditions, and the rule that no stage failure can crash
the workflow or put prose into a reason code.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.investigation import (
    Investigation,
    InvestigationBudget,
    StageName,
    StageStatus,
    StoppingCondition,
)
from flakepilot.diagnosis.application.store import AiPolicy
from flakepilot.diagnosis.domain.output import DataPolicy
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.integrations.application.ports import (
    AiAssistPort,
    AiCapabilities,
    AiRequest,
    AiResponse,
    ResearchProviderError,
    ResearchQuery,
)
from flakepilot.integrations.infrastructure.ai import FakeAiAssist, NullAiAssist
from flakepilot.shared_kernel.events import ActorType, Channel, Event, Origin
from flakepilot.test_intelligence.application.prioritization import prioritize
from flakepilot.test_intelligence.application.reliability import (
    Reliability,
)
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedCase,
    ClassifiedRunReport,
    RunViewer,
)
from flakepilot.test_intelligence.application.store import (
    IdentityHistory,
    ReportedCase,
    RunReport,
    RunSummary,
)
from flakepilot.test_intelligence.domain.classification import Observation
from flakepilot.test_intelligence.domain.results import Outcome, RunSource

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
RUN = "cccccccc-cccc-cccc-cccc-cccccccccccc"
ACTOR = "dddddddd-dddd-dddd-dddd-dddddddddddd"
TEST = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
NOW = datetime.now(UTC)
ORIGIN = Origin(Channel.CLI, "invest-test")


def _report() -> ClassifiedRunReport:
    run = RunReport(
        run_id=RUN,
        org_id=ORG,
        repo_id=REPO,
        upload_id="u",
        source="ci_upload",
        commit_sha="a" * 40,
        environment="ci-linux",
        revision_status="unchecked",
        received_at=NOW,
        summary=RunSummary(
            tests=2, attempts=3, passed=1, failed=1, errored=0, skipped=0
        ),
        metadata=None,
        content_sha256=None,
        size_bytes=None,
        artifact_key=None,
        results=(
            ReportedCase("tests.a", "test_flaky", ("failed", "passed"), TEST),
            ReportedCase(
                "tests.a",
                "test_green",
                ("passed",),
                "11111111-2222-3333-4444-555555555555",
            ),
        ),
    )
    cases = (
        ClassifiedCase(
            case=ReportedCase("tests.a", "test_flaky", ("failed", "passed"), TEST),
            classification=None,
        ),
        ClassifiedCase(
            case=ReportedCase(
                "tests.a",
                "test_green",
                ("passed",),
                "11111111-2222-3333-4444-555555555555",
            ),
            classification=None,
        ),
    )
    return ClassifiedRunReport(run=run, cases=cases)


def _one_flaky_history() -> tuple[IdentityHistory, ...]:
    return (
        IdentityHistory(
            test_id=TEST,
            classname="tests.a",
            name="test_flaky",
            observations=(
                Observation(
                    run_id="r1",
                    commit_sha="a" * 40,
                    environment="ci-linux",
                    received_at=NOW,
                    source=RunSource.CI_UPLOAD,
                    attempts=(Outcome.FAILED, Outcome.PASSED),
                ),
            ),
        ),
    )


class FakeRunViewer:
    """RunViewer stand-in: a canned report or a refusal."""

    def __init__(self, outcome: ClassifiedRunReport | Exception) -> None:
        self._outcome = outcome

    async def classified_report_for(
        self, *args: object, **kwargs: object
    ) -> ClassifiedRunReport:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeDiagStore:
    def __init__(self, policy: AiPolicy) -> None:
        self._policy = policy
        self.events: list[Event] = []
        self.requests: list[object] = []

    async def policy(self, org_id: str) -> AiPolicy:
        del org_id
        return self._policy

    async def set_policy(self, org_id: str, policy: AiPolicy) -> None:
        del org_id, policy

    async def record_request(self, request: object) -> None:
        self.requests.append(request)

    async def finish_request(self, request: object) -> None:
        self.requests[-1] = request

    async def append_event(self, event: Event) -> None:
        self.events.append(event)


class FakeDiagUow:
    def __init__(self, policy: AiPolicy) -> None:
        self.store = FakeDiagStore(policy)

    def __call__(self) -> Any:
        return self

    async def __aenter__(self) -> FakeDiagStore:
        return self.store

    async def __aexit__(self, *args: object) -> None:
        del args


class FakeReliability:
    """Reliability stand-in with canned answers and a cutoff memory."""

    def __init__(self, report_outcome: Any, ranking_outcome: Any) -> None:
        self._report_outcome = report_outcome
        self._ranking_outcome = ranking_outcome
        self.cutoffs: list[datetime] = []

    async def report(self, *, org_id: str, repo_id: str, cutoff: datetime) -> Any:
        del org_id, repo_id
        self.cutoffs.append(cutoff)
        if isinstance(self._report_outcome, Exception):
            raise self._report_outcome
        return self._report_outcome

    async def prioritized(self, *, org_id: str, repo_id: str, cutoff: datetime) -> Any:
        del org_id, repo_id, cutoff
        if isinstance(self._ranking_outcome, Exception):
            raise self._ranking_outcome
        return self._ranking_outcome


class RecordingPort:
    """Records assist requests, then answers from a canned delegate."""

    def __init__(self, delegate: AiAssistPort) -> None:
        self._delegate = delegate
        self.requests: list[AiRequest] = []

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider="fake")

    async def submit(self, request: AiRequest) -> AiResponse:
        self.requests.append(request)
        return await self._delegate.submit(request)


class RecordedResearch:
    """A canned `ResearchProvider` that records what it was asked."""

    def __init__(self, outcome: Any = ()) -> None:
        self._outcome = outcome
        self.queries: list[ResearchQuery] = []

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider="test_research")

    async def search(self, query: ResearchQuery) -> tuple[object, ...]:
        self.queries.append(query)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return cast(tuple[object, ...], self._outcome)


def _investigation(
    run_outcome: ClassifiedRunReport | Exception | None = None,
    *,
    research: Any = None,
    port: AiAssistPort | None = None,
    policy: AiPolicy | None = None,
    reliability: FakeReliability | None = None,
    budget: InvestigationBudget | None = None,
) -> tuple[Any, FakeDiagUow, Any]:
    uow = FakeDiagUow(policy or AiPolicy(DataPolicy.DISABLED, False))
    spied: Any = RecordingPort(port if port is not None else NullAiAssist())
    workflow = Investigation(
        runs=cast(RunViewer, FakeRunViewer(run_outcome or _report())),
        assist=Assist(cast(Any, uow), spied),
        reliability=cast(
            Reliability, reliability or FakeReliability(None, prioritize(()))
        ),
        unit_of_work=cast(Any, uow),
        research=cast(Any, research),
        budget=budget,
    )
    return workflow, uow, spied


def _access(*grants: Grant) -> OrgAccess:
    return OrgAccess(
        ACTOR, ORG, org_admin=False, grants=frozenset((REPO, g) for g in grants)
    )


def _run(workflow: Any, access: OrgAccess, **overrides: object) -> Any:
    return asyncio.run(
        workflow.investigate(
            access,
            org_id=ORG,
            repo_id=REPO,
            run_id=RUN,
            code_host=CodeHostAccess.READ,
            cutoff=NOW,
            origin=ORIGIN,
            **overrides,
        )
    )


def test_the_happy_path_produces_all_stages_and_a_provenance_event() -> None:
    workflow, uow, _ = _investigation()
    report = _run(workflow, _access(Grant.INVESTIGATE))
    assert report.stopping is StoppingCondition.COMPLETE
    assert [stage.name for stage in report.stages] == [
        StageName.COLLECT,
        StageName.RESEARCH,
        StageName.HYPOTHESIZE,
        StageName.PRIORITIZE,
    ]
    # Nothing wired beyond the collect path: every optional stage is skipped.
    assert report.stages[1].status is StageStatus.SKIPPED
    assert report.stages[1].reason == "provider_absent"
    assert report.stages[2].reason == "policy_disabled"
    assert report.stages[3].reason == "no_history_at_cutoff"
    # The assist's own event plus exactly one investigation provenance event.
    assert [event.name for event in uow.store.events] == [
        "diag.diagnosis.recorded",
        "diag.investigation.recorded",
    ]
    event = uow.store.events[-1]
    assert event.after == "complete"
    assert event.payload["stages_produced"] == 1
    assert event.payload["stages_skipped"] == 3
    assert event.payload["stages_failed"] == 0


def test_all_four_stages_produce_when_everything_is_wired() -> None:
    research = RecordedResearch()
    ranking = prioritize(_one_flaky_history())
    reliability = FakeReliability(None, ranking)
    workflow, uow, spied = _investigation(
        research=research,
        port=FakeAiAssist(),
        policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
        reliability=reliability,
    )
    report = _run(workflow, _access(Grant.INVESTIGATE))
    assert [stage.status for stage in report.stages] == [StageStatus.PRODUCED] * 4
    assert len(research.queries) == 1
    assert "test_flaky" in research.queries[0].query
    assert research.queries[0].max_sources == 5  # the default budget
    # The assist ran through the policy gate with evidence framed as parts.
    assert len(spied.requests) == 1 and len(spied.requests[0].data_parts) == 2
    assert any("test_flaky" in part.text for part in spied.requests[0].data_parts)
    assert report.assist is not None and report.assist.linked == 0
    assert report.prioritization is ranking
    assert uow.store.events[-1].payload["stages_produced"] == 4


def test_the_budget_caps_evidence_parts() -> None:
    workflow, _, spied = _investigation(
        port=FakeAiAssist(),
        policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
        budget=InvestigationBudget(max_evidence_parts=1),
    )
    _run(workflow, _access(Grant.INVESTIGATE))
    assert len(spied.requests[0].data_parts) == 1  # two cases in, one sent


def test_a_missing_run_stops_with_not_found_and_still_records() -> None:
    workflow, uow, _ = _investigation(ResourceNotFound())
    with pytest.raises(ResourceNotFound):
        _run(workflow, _access(Grant.INVESTIGATE))
    assert [
        event.after
        for event in uow.store.events
        if event.name == "diag.investigation.recorded"
    ] == ["not_found"]
    assert uow.store.events[0].payload["sources"] == 0


def test_a_denied_grant_stops_with_unauthorized_and_records() -> None:
    workflow, uow, _ = _investigation()
    with pytest.raises(PermissionDenied):
        _run(workflow, _access(Grant.VIEW))  # view alone cannot investigate
    assert [
        event.after
        for event in uow.store.events
        if event.name == "diag.investigation.recorded"
    ] == ["unauthorized"]


def test_a_refused_research_provider_is_an_honest_skip() -> None:
    workflow, _, _ = _investigation(
        research=RecordedResearch(ResearchProviderError("quota_denied"))
    )
    report = _run(workflow, _access(Grant.INVESTIGATE))
    assert report.stages[1].status is StageStatus.SKIPPED
    assert report.stages[1].reason == "quota_denied"
    assert report.stopping is StoppingCondition.COMPLETE


def test_a_research_crash_is_recorded_never_raised() -> None:
    class ExplodingResearch(RecordedResearch):
        async def search(self, query: ResearchQuery) -> tuple[object, ...]:
            self.queries.append(query)
            raise RuntimeError("internal provider traceback")

    workflow, uow, _ = _investigation(research=ExplodingResearch())
    report = _run(workflow, _access(Grant.INVESTIGATE))
    assert report.stages[1].status is StageStatus.FAILED
    assert report.stages[1].reason == "stage_error"
    assert report.stopping is StoppingCondition.COMPLETE
    assert uow.store.events[-1].payload["stages_failed"] == 1


def test_a_prioritization_crash_is_recorded_never_raised() -> None:
    reliability = FakeReliability(RuntimeError("database was sad"), None)
    workflow, _, _ = _investigation(reliability=reliability)
    report = _run(workflow, _access(Grant.INVESTIGATE))
    assert report.stages[3].status is StageStatus.FAILED
    assert report.stages[3].reason == "stage_error"
    assert report.reliability is None and report.prioritization is None
    assert report.stopping is StoppingCondition.COMPLETE


def test_nothing_failing_skips_research_without_a_call() -> None:
    report = _report()
    report = ClassifiedRunReport(
        run=report.run,
        cases=(
            ClassifiedCase(
                case=ReportedCase("tests.a", "test_green", ("passed",), TEST),
                classification=None,
            ),
        ),
    )
    research = RecordedResearch()
    workflow, _, _ = _investigation(run_outcome=report, research=research)
    outcome = _run(workflow, _access(Grant.INVESTIGATE))
    assert outcome.stages[1].status is StageStatus.SKIPPED
    assert outcome.stages[1].reason == "nothing_failing"
    assert research.queries == []  # never called for an all-green run


def test_the_provenance_event_carries_only_short_codes_and_counts() -> None:
    workflow, uow, _ = _investigation()
    _run(workflow, _access(Grant.INVESTIGATE))
    event = uow.store.events[-1]
    assert all(
        isinstance(value, (str, int, bool, type(None)))
        for value in event.payload.values()
    )
    assert all(len(str(value)) <= 100 for value in event.payload.values())
    assert event.subject.type == "investigation" and uuid.UUID(event.subject.id)
    assert event.org_id == ORG and event.repo_id == REPO


def test_every_stage_status_maps_from_the_assist_outcome() -> None:
    workflow, _, _ = _investigation(
        port=FakeAiAssist(),
        policy=AiPolicy(DataPolicy.EVIDENCE_ONLY, False),
    )
    report = _run(workflow, _access(Grant.INVESTIGATE))
    assert report.stages[2].status is StageStatus.PRODUCED


def test_a_delegated_agent_run_attributes_the_delegator_with_the_agent_code() -> None:
    """Goal 9 attribution: the actor is the human delegator; the agent code
    rides in the payload, short and opaque — no agent identity of its own."""
    from flakepilot.identity.application.agents import AgentAccess

    workflow, uow, _ = _investigation()
    agent = AgentAccess(
        principal_id=ACTOR,  # the delegator's verified user ID
        org_id=ORG,
        grants=frozenset({(REPO, Grant.INVESTIGATE)}),
        agent_id="helper",
        repo_id=REPO,
        expires_at=NOW + timedelta(hours=1),
    )
    _run(workflow, agent)
    event = uow.store.events[-1]
    assert event.actor.type is ActorType.USER and event.actor.id == ACTOR
    assert event.payload["via_agent"] == "helper"
