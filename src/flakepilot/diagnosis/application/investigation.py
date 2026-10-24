"""The bounded investigation workflow (ADR-017 decisions 1, 2, and 7).

Goal 2 (agent-shaped orchestration) without a second workflow authority: the
work is a typed, local stage pipeline in DIA's application layer — collect,
research (optional), hypothesize (optional, policy-gated), prioritize, then
one provenance record. Stages have budgets; a stage that cannot run is
recorded as skipped, never silently dropped. No stage can mutate grants,
execute code, or replace a deterministic verdict: they read TI's public
application API, and the assist stage runs entirely through its own policy
gate and authorization check.

Goal 15 (provenance): the outcome is appended to DIA's outbox exactly like
every other diagnosis event — IDs, enums and counts only, never secret
values or raw untrusted text (the envelope contract enforces this).
Externally attested provenance (goal 15's external step) remains a later,
clearly separate stage.

The workflow never reads a clock: the caller supplies the analytics `cutoff`
(the leakage guard for history and prioritization). Stopping conditions are
explicit: the investigation completes, is refused (`unauthorized`), or finds
nothing (`not_found`) — and each stop is recorded before the exception stops
propagating.
"""

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from flakepilot.diagnosis.application.assist import (
    MAX_PARTS,
    Assist,
    AssistRejected,
    AssistResult,
    UntrustedPart,
)
from flakepilot.diagnosis.application.store import DiagStore
from flakepilot.diagnosis.domain.output import DiagnosisOutcome
from flakepilot.identity.application.authorization import (
    CodeHostAccess,
    Grant,
    OrgAccess,
    PermissionDenied,
    ResourceNotFound,
    authorize,
)
from flakepilot.integrations.application.ports import (
    ResearchProvider,
    ResearchProviderError,
    ResearchQuery,
    ResearchSource,
)
from flakepilot.shared_kernel.events import Actor, ActorType, Event, Origin, Subject
from flakepilot.test_intelligence.application.prioritization import Prioritization
from flakepilot.test_intelligence.application.reliability import (
    Reliability,
    ReliabilityReport,
)
from flakepilot.test_intelligence.application.run_view import (
    ClassifiedRunReport,
    RunViewer,
)

_logger = logging.getLogger("flakepilot.diagnosis")

UnitOfWork = Callable[[], AbstractAsyncContextManager[DiagStore]]

INSTRUCTIONS = (
    "You are assisting an automated CI investigation. The deterministic "
    "classification is authoritative and never overridden; your claims are "
    "advisory only. Reply as JSON: hypotheses: [{summary, cause, citations, "
    "confidence}]. Emit evidence_id citations that refer only to the IDs you "
    "are given as evidence."
)

MAX_QUERY_BYTES = 512


class StageName(StrEnum):
    COLLECT = "collect"
    RESEARCH = "research"
    HYPOTHESIZE = "hypothesize"
    PRIORITIZE = "prioritize"


class StageStatus(StrEnum):
    PRODUCED = "produced"
    SKIPPED = (
        "skipped"  # nothing to run it with or the provider refused: a normal answer
    )
    FAILED = "failed"  # an unexpected stage failure: recorded, never crashed


class StoppingCondition(StrEnum):
    COMPLETE = "complete"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"


@dataclass(frozen=True)
class InvestigationBudget:
    """Caps, not hopes. Every stage measures its own input against these."""

    max_evidence_parts: int = 8
    max_sources: int = 5
    max_history_tests: int = 25

    def __post_init__(self) -> None:
        if not 1 <= self.max_evidence_parts <= min(MAX_PARTS, 20):
            raise ValueError(f"max_evidence_parts is 1-{min(MAX_PARTS, 20)}")
        if not 1 <= self.max_sources <= 10:
            raise ValueError("max_sources is 1-10")
        if not 1 <= self.max_history_tests <= 100:
            raise ValueError("max_history_tests is 1-100")


@dataclass(frozen=True)
class StageRecord:
    """One stage's outcome, for the report and the provenance event."""

    name: StageName
    status: StageStatus
    reason: str | None = None


@dataclass(frozen=True)
class InvestigationReport:
    """The composed result. Deterministic verdicts come verbatim from TI; the
    assist stage contributes counts only (the ADR-014 rule) — hypothesis text
    stays in the diagnosis store's bounded record, never in this report."""

    org_id: str
    repo_id: str
    run_id: str
    run: ClassifiedRunReport
    stages: tuple[StageRecord, ...]
    stopping: StoppingCondition
    sources: tuple[ResearchSource, ...]
    assist: AssistResult | None
    reliability: ReliabilityReport | None
    prioritization: Prioritization | None


class Investigation:
    """Orchestrates one investigation of one stored run, bounded and audited.

    Authorization happens at the entry (`repo.investigate`) and again inside
    the assist use case (its own gate); the pipeline itself has no bypass.
    """

    def __init__(
        self,
        *,
        runs: RunViewer,
        assist: Assist,
        reliability: Reliability,
        unit_of_work: UnitOfWork,
        research: ResearchProvider | None = None,
        budget: InvestigationBudget | None = None,
    ) -> None:
        self._runs = runs
        self._assist = assist
        self._reliability = reliability
        self._unit_of_work = unit_of_work
        self._research = research
        self._budget = budget or InvestigationBudget()

    async def investigate(
        self,
        access: OrgAccess,
        *,
        org_id: str,
        repo_id: str,
        run_id: str,
        code_host: CodeHostAccess,
        cutoff: datetime,
        origin: Origin,
    ) -> InvestigationReport:
        """Needs `repo.investigate`. Absent providers skip their stages; a
        missing run or a denied grant stops the workflow itself — recorded."""
        try:
            authorize(
                access,
                Grant.INVESTIGATE,
                org_id=org_id,
                repo_id=repo_id,
                code_host=code_host,
            )
            run = await self._runs.classified_report_for(
                access,
                org_id=org_id,
                repo_id=repo_id,
                run_id=run_id,
                code_host=code_host,
            )
        except ResourceNotFound:
            await self._provenance(
                access,
                origin,
                org_id,
                repo_id,
                run_id,
                stopping=StoppingCondition.NOT_FOUND,
                stages=(),
                sources=0,
                links=0,
            )
            raise
        except PermissionDenied:
            await self._provenance(
                access,
                origin,
                org_id,
                repo_id,
                run_id,
                stopping=StoppingCondition.UNAUTHORIZED,
                stages=(),
                sources=0,
                links=0,
            )
            raise

        stages = [StageRecord(StageName.COLLECT, StageStatus.PRODUCED)]

        sources, record = await self._research_stage(run, origin)
        stages.append(record)

        assist_result, record = await self._hypothesize_stage(
            access,
            run,
            org_id=org_id,
            repo_id=repo_id,
            code_host=code_host,
            origin=origin,
        )
        stages.append(record)

        reliability, ranking, record = await self._prioritize_stage(
            org_id, repo_id, cutoff
        )
        stages.append(record)

        await self._provenance(
            access,
            origin,
            org_id,
            repo_id,
            run_id,
            stopping=StoppingCondition.COMPLETE,
            stages=tuple(stages),
            sources=len(sources),
            links=assist_result.linked if assist_result else 0,
        )
        return InvestigationReport(
            org_id=org_id,
            repo_id=repo_id,
            run_id=run_id,
            run=run,
            stages=tuple(stages),
            stopping=StoppingCondition.COMPLETE,
            sources=sources,
            assist=assist_result,
            reliability=reliability,
            prioritization=ranking,
        )

    def _collect(self, run: ClassifiedRunReport) -> StageRecord:
        del run  # the collected report itself is the stage output
        return StageRecord(StageName.COLLECT, StageStatus.PRODUCED)

    def _evidence_parts(self, run: ClassifiedRunReport) -> tuple[UntrustedPart, ...]:
        """The run's cases as untrusted evidence text, capped by the budget."""
        parts: list[UntrustedPart] = []
        for entry in run.cases[: self._budget.max_evidence_parts]:
            attempts = " ".join(entry.case.attempts)
            parts.append(
                UntrustedPart(
                    name="case",
                    text=(
                        f"classname={entry.case.classname} name={entry.case.name} "
                        f"test_id={entry.case.test_id} attempts=[{attempts}]"
                    ),
                )
            )
        return tuple(parts)

    def _evidence_ids(self, run: ClassifiedRunReport) -> frozenset[str]:
        return frozenset(entry.case.test_id for entry in run.cases)

    def _research_query(self, run: ClassifiedRunReport) -> str | None:
        """The failing cases, named — capped, or None when nothing failed.

        `ReportedCase.attempts` are the report's own outcome strings; the
        failing ones are the TI rule set's `failed` and `errored` values.
        """
        failing = [
            entry.case.name
            for entry in run.cases
            if entry.case.attempts
            and any(attempt in ("failed", "errored") for attempt in entry.case.attempts)
        ]
        if not failing:
            return None
        return " ".join(failing)[:MAX_QUERY_BYTES]

    async def _research_stage(
        self, run: ClassifiedRunReport, origin: Origin
    ) -> tuple[tuple[ResearchSource, ...], StageRecord]:
        del origin
        if self._research is None:
            return (), StageRecord(
                StageName.RESEARCH, StageStatus.SKIPPED, "provider_absent"
            )
        query = self._research_query(run)
        if query is None:
            return (), StageRecord(
                StageName.RESEARCH, StageStatus.SKIPPED, "nothing_failing"
            )
        try:
            sources = await self._research.search(
                ResearchQuery(query, max_sources=self._budget.max_sources)
            )
        except ResearchProviderError as error:
            return (), StageRecord(
                StageName.RESEARCH, StageStatus.SKIPPED, error.reason
            )
        except Exception:
            _logger.exception("research stage failed")
            return (), StageRecord(
                StageName.RESEARCH, StageStatus.FAILED, "stage_error"
            )
        return sources, StageRecord(StageName.RESEARCH, StageStatus.PRODUCED)

    async def _hypothesize_stage(
        self,
        access: OrgAccess,
        run: ClassifiedRunReport,
        *,
        org_id: str,
        repo_id: str,
        code_host: CodeHostAccess,
        origin: Origin,
    ) -> tuple[AssistResult | None, StageRecord]:
        parts = self._evidence_parts(run)
        try:
            result = await self._assist.assist(
                access,
                org_id=org_id,
                repo_id=repo_id,
                code_host=code_host,
                instructions=INSTRUCTIONS,
                parts=parts,
                evidence_ids=self._evidence_ids(run),
                origin=origin,
            )
        except PermissionDenied:
            # The entry authorize() passed; a mismatch here is a defect, recorded.
            _logger.error("assist denied after the entry grant")
            return None, StageRecord(
                StageName.HYPOTHESIZE, StageStatus.FAILED, "unauthorized"
            )
        except AssistRejected as error:
            _logger.info("assist refused: %s", error.reason)
            return None, StageRecord(
                StageName.HYPOTHESIZE, StageStatus.SKIPPED, error.reason
            )
        except Exception:
            _logger.exception("hypothesize stage failed")
            return None, StageRecord(
                StageName.HYPOTHESIZE, StageStatus.FAILED, "stage_error"
            )
        if result.outcome in (
            DiagnosisOutcome.PRODUCED,
            DiagnosisOutcome.NO_HYPOTHESIS,
        ):
            return result, StageRecord(
                StageName.HYPOTHESIZE,
                StageStatus.PRODUCED,
                None
                if result.outcome is DiagnosisOutcome.PRODUCED
                else "no_hypothesis",
            )
        if result.outcome is DiagnosisOutcome.REJECTED:
            return result, StageRecord(
                StageName.HYPOTHESIZE, StageStatus.FAILED, result.reason
            )
        # UNAVAILABLE (policy disabled, model down): a recorded skip, not an error.
        return result, StageRecord(
            StageName.HYPOTHESIZE, StageStatus.SKIPPED, result.reason
        )

    async def _prioritize_stage(
        self, org_id: str, repo_id: str, cutoff: datetime
    ) -> tuple[ReliabilityReport | None, Prioritization | None, StageRecord]:
        try:
            reliability = await self._reliability.report(
                org_id=org_id, repo_id=repo_id, cutoff=cutoff
            )
            ranking = await self._reliability.prioritized(
                org_id=org_id, repo_id=repo_id, cutoff=cutoff
            )
        except Exception:
            _logger.exception("prioritize stage failed")
            return (
                None,
                None,
                StageRecord(StageName.PRIORITIZE, StageStatus.FAILED, "stage_error"),
            )
        record = StageRecord(
            StageName.PRIORITIZE,
            StageStatus.PRODUCED if ranking.ranked else StageStatus.SKIPPED,
            None if ranking.ranked else "no_history_at_cutoff",
        )
        return reliability, ranking, record

    async def _provenance(
        self,
        access: OrgAccess,
        origin: Origin,
        org_id: str,
        repo_id: str,
        run_id: str,
        stopping: StoppingCondition,
        stages: tuple[StageRecord, ...],
        sources: int,
        links: int,
    ) -> None:
        """One durable provenance record for the investigation (goal 15)."""
        produced = sum(1 for stage in stages if stage.status is StageStatus.PRODUCED)
        skipped = sum(1 for stage in stages if stage.status is StageStatus.SKIPPED)
        failed = sum(1 for stage in stages if stage.status is StageStatus.FAILED)
        event = Event(
            name="diag.investigation.recorded",
            actor=Actor(ActorType.USER, access.principal_id),
            origin=origin,
            subject=Subject("investigation", str(uuid.uuid4())),
            org_id=org_id,
            repo_id=repo_id,
            after=stopping.value,
            payload={
                "stages_produced": produced,
                "stages_skipped": skipped,
                "stages_failed": failed,
                "sources": sources,
                "linked": links,
            },
        )
        try:
            async with self._unit_of_work() as store:
                await store.append_event(event)
        except Exception:
            _logger.exception("provenance event could not be appended")
            raise
