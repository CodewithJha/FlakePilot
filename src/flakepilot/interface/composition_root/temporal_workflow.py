"""The Temporal workflow of the goal-5 proof of concept: one bounded investigation.

Kept free of FlakePilot imports so Temporal's workflow sandbox can load it on
its own. The workflow only schedules the activity; FlakePilot stays the
authority (an advisory lock and the existing `diag.outbox` row decide whether
the investigation runs, in `temporal_investigation`). Inputs and results are
IDs, enum codes and counts: that is all Temporal's history ever holds.

Retry policy: at most 3 attempts, and only the transient reason codes retry;
every other `ApplicationError` the activity raises is non-retryable.
Cancelling the workflow cancels the activity (delivered through its
heartbeat) and waits for it to stop.
"""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.workflow import ActivityCancellationType

TASK_QUEUE = "flakepilot-investigations"
WORKFLOW = "flakepilot.InvestigationWorkflow"
ACTIVITY = "flakepilot.run_investigation"
TRANSIENT = ("database_unavailable", "investigation_in_progress")
FINAL = (
    "invalid_request",
    "not_found",
    "unauthorized",
    "investigation_failed",
)
START_TO_CLOSE = timedelta(seconds=120)
MAX_HEARTBEAT_SECONDS = 30
MIN_HEARTBEAT_SECONDS = 2
MAX_ATTEMPTS = 3
ID_REUSE = WorkflowIDReusePolicy.REJECT_DUPLICATE


@dataclass(frozen=True)
class InvestigationRequest:
    """`key` (a UUID) names the investigation: workflow ID, lock, and the
    `Origin` correlation ID of its provenance row."""

    key: str
    org_id: str
    run_id: str
    member_github_id: str
    heartbeat_timeout_seconds: int = MAX_HEARTBEAT_SECONDS


@dataclass(frozen=True)
class InvestigationOutcome:
    key: str
    stopping: str
    stages_produced: int
    replayed: bool  # True: the recorded outcome was returned, nothing ran


def workflow_id(key: str) -> str:
    return f"investigation-{key}"


@workflow.defn(name=WORKFLOW)
class InvestigationWorkflow:
    @workflow.run
    async def run(self, request: InvestigationRequest) -> InvestigationOutcome:
        heartbeat = min(
            max(request.heartbeat_timeout_seconds, MIN_HEARTBEAT_SECONDS),
            MAX_HEARTBEAT_SECONDS,
        )
        outcome: InvestigationOutcome = await workflow.execute_activity(
            ACTIVITY,
            request,
            result_type=InvestigationOutcome,
            start_to_close_timeout=START_TO_CLOSE,
            heartbeat_timeout=timedelta(seconds=heartbeat),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
                maximum_attempts=MAX_ATTEMPTS,
                non_retryable_error_types=list(FINAL),
            ),
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        return outcome
