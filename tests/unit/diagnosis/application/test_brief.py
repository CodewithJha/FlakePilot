"""The spoken investigation brief (goal 14): counts in, no repository text out."""

import asyncio
from datetime import UTC, datetime

import pytest

from flakepilot.diagnosis.application.assist import AssistResult
from flakepilot.diagnosis.application.brief import (
    MAX_BRIEF_BYTES,
    SpokenBriefUnavailable,
    compose_brief,
    speak_brief,
)
from flakepilot.diagnosis.application.investigation import (
    InvestigationReport,
    StageName,
    StageRecord,
    StageStatus,
    StoppingCondition,
)
from flakepilot.diagnosis.domain.output import DiagnosisOutcome
from flakepilot.integrations.application.ports import (
    AiCapabilities,
    ResearchSource,
    SpeechAudio,
    SpeechProviderError,
    SpeechRequest,
)
from flakepilot.integrations.infrastructure.speech import RecordedSpeechProvider
from flakepilot.test_intelligence.application.prioritization import (
    Prioritization,
    Priority,
)
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedCase,
    ClassifiedRunReport,
)
from flakepilot.test_intelligence.application.store import (
    ReportedCase,
    RunReport,
    RunSummary,
)
from flakepilot.test_intelligence.domain.classification import (
    Classification,
    FailureClass,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)
LINK = "https://flakepilot.example.com/runs/cccccccc"
SECRET = "AWS_SECRET_ACCESS_KEY=abc123"


def _case(
    name: str, attempts: tuple[str, ...], verdict: FailureClass | None
) -> ClassifiedCase:
    return ClassifiedCase(
        case=ReportedCase("tests.mod", name, attempts, f"id-{name}"),
        classification=None
        if verdict is None
        else Classification(failure_class=verdict, reason=None, target_sha=None),
    )


def _report(
    cases: tuple[ClassifiedCase, ...],
    *,
    stages: tuple[StageRecord, ...] | None = None,
    assist: AssistResult | None = None,
    ranked: int = 0,
) -> InvestigationReport:
    run = RunReport(
        run_id="run",
        org_id="org",
        repo_id="repo",
        upload_id="u",
        source="ci_upload",
        commit_sha="a" * 40,
        environment="ci",
        revision_status="unchecked",
        received_at=NOW,
        summary=RunSummary(
            tests=len(cases), attempts=0, passed=0, failed=0, errored=0, skipped=0
        ),
        metadata=None,
        content_sha256=None,
        size_bytes=None,
        artifact_key=None,
        results=tuple(case.case for case in cases),
    )
    priorities = tuple(
        Priority(
            rank=index + 1,
            test_id=f"t{index}",
            classname="c",
            name=SECRET,
            flaky_runs=1,
            failed_attempts=1,
            attempts=2,
            last_failure_at=NOW,
            last_run_at=NOW,
        )
        for index in range(ranked)
    )
    return InvestigationReport(
        org_id="org",
        repo_id="repo",
        run_id="run",
        run=ClassifiedRunReport(run=run, cases=cases),
        stages=stages
        or (
            StageRecord(StageName.COLLECT, StageStatus.PRODUCED),
            StageRecord(StageName.RESEARCH, StageStatus.SKIPPED, "provider_absent"),
            StageRecord(StageName.HYPOTHESIZE, StageStatus.PRODUCED),
            StageRecord(StageName.PRIORITIZE, StageStatus.FAILED, "stage_error"),
        ),
        stopping=StoppingCondition.COMPLETE,
        sources=(
            ResearchSource(
                url="https://docs.python.org/x",
                title=SECRET,
                retrieved_at=NOW,
                excerpt=SECRET,
            ),
        ),
        assist=assist,
        reliability=None,
        prioritization=Prioritization(ranked=priorities, note=SECRET),
    )


MIXED = (
    _case("test_flaky_" + SECRET, ("failed", "passed"), FailureClass.FLAKY),
    _case(
        "test_regressed", ("failed", "failed"), FailureClass.DETERMINISTIC_REGRESSION
    ),
    _case("test_open", ("errored",), FailureClass.INCONCLUSIVE),
    _case("test_new", ("passed",), None),
)


def test_the_brief_states_verdicts_evidence_uncertainty_steps_and_link() -> None:
    assist = AssistResult(
        "r", DiagnosisOutcome.PRODUCED, None, linked=2, unverified=1, contradicted=0
    )
    brief = compose_brief(_report(MIXED, assist=assist, ranked=3), report_url=LINK)
    assert brief.startswith("FlakePilot investigation brief. ")
    assert (
        "Verdict: 1 deterministic regression, 1 flaky, 1 inconclusive; "
        "1 test without a verdict." in brief
    )
    assert (
        "Evidence: 4 tests in the run, 3 with failing attempts, 1 research source, "
        "2 model claims linked to evidence." in brief
    )
    assert "1 of 4 stages skipped and 1 failed; 1 model claim unverified" in brief
    assert "Start with the deterministic regressions." in brief
    assert "Stabilize or quarantine the flaky tests." in brief
    assert "Collect more runs to settle the open verdicts." in brief
    assert "History ranks 3 tests for attention." in brief
    assert brief.endswith(f"Full report: {LINK}")


def test_no_repository_or_provider_text_reaches_the_brief() -> None:
    brief = compose_brief(_report(MIXED, ranked=1), report_url=LINK)
    assert SECRET not in brief and "test_flaky" not in brief and "test_new" not in brief
    assert "docs.python.org" not in brief


def test_open_verdicts_ask_for_runs_and_an_empty_run_needs_nothing() -> None:
    clean = (_case("test_ok", ("passed",), None),)
    brief = compose_brief(_report(clean), report_url="http://127.0.0.1:8000/runs/r")
    assert "Verdict: no failure verdicts; 1 test without a verdict." in brief
    assert "Collect more runs" in brief
    no_open = compose_brief(_report(()), report_url=LINK)
    assert "No action is needed for this run." in no_open


def test_the_brief_is_bounded_even_for_huge_runs() -> None:
    many = tuple(_case(f"t{n}", ("failed",), FailureClass.FLAKY) for n in range(5_000))
    brief = compose_brief(_report(many, ranked=100), report_url=LINK)
    assert len(brief.encode()) <= MAX_BRIEF_BYTES
    SpeechRequest(brief)  # fits the port


@pytest.mark.parametrize(
    "link",
    [
        "https://host/runs/r?token=abc",
        "https://host/runs/r#frag",
        "https://user:pw@host/runs/r",
        "javascript:alert(1)",
        "https://host/" + "x" * 200,
        "https://host/a b",
        "",
    ],
)
def test_unsafe_report_links_are_refused(link: str) -> None:
    with pytest.raises(ValueError):
        compose_brief(_report(MIXED), report_url=link)


class SpyingSpeech:
    def __init__(self) -> None:
        self.requests: list[SpeechRequest] = []

    def describe(self) -> AiCapabilities:
        return AiCapabilities(available=True, provider="spy")

    async def speak(self, request: SpeechRequest) -> SpeechAudio:
        self.requests.append(request)
        return SpeechAudio(
            audio=b"x", media_type="audio/mpeg", provider="spy", model=None
        )


def test_speaking_sends_exactly_the_composed_brief() -> None:
    report = _report(MIXED)
    spy = SpyingSpeech()
    asyncio.run(speak_brief(report, speech=spy, report_url=LINK, voice="calm"))
    assert [request.text for request in spy.requests] == [
        compose_brief(report, report_url=LINK)
    ]
    assert spy.requests[0].voice == "calm"


def test_the_fixture_speaks_a_brief() -> None:
    audio = asyncio.run(
        speak_brief(_report(MIXED), speech=RecordedSpeechProvider(), report_url=LINK)
    )
    assert audio.media_type == "audio/wav"


def test_a_speech_refusal_keeps_only_its_short_code() -> None:
    class Refusing(SpyingSpeech):
        async def speak(self, request: SpeechRequest) -> SpeechAudio:
            raise SpeechProviderError("quota_exceeded")

    with pytest.raises(SpokenBriefUnavailable) as caught:
        asyncio.run(speak_brief(_report(MIXED), speech=Refusing(), report_url=LINK))
    assert caught.value.reason == "quota_exceeded"
    assert caught.value.__cause__ is None
