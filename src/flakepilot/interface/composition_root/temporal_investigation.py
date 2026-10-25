"""Temporal proof of concept (goal 5): one bounded investigation, FlakePilot authoritative.

Opt-in operator commands only; the API process and the default `worker` never
import this module or connect to Temporal:

    python -m flakepilot.interface.composition_root temporal-worker
    python -m flakepilot.interface.composition_root temporal-run-investigation \\
        --org-id <uuid> --run-id <uuid> --member-github-id <id> [--key <uuid>]
    python -m flakepilot.interface.composition_root temporal-cancel-investigation \\
        --key <uuid>

They need `FLAKEPILOT_TEMPORAL_ADDRESS` and `FLAKEPILOT_TEMPORAL_NAMESPACE`
(plus `FLAKEPILOT_TEMPORAL_API_KEY`, which turns TLS on, for Temporal Cloud).

Temporal schedules, retries and cancels; FlakePilot decides. The activity
(`InvestigationActivity.run`):

1. takes a session-level PostgreSQL advisory lock on the investigation key, on
   its own connection, closed when the activity ends (so a killed worker's
   lock goes with its connection). Held elsewhere: `investigation_in_progress`
   (retryable);
2. returns the recorded outcome, without running anything, when `diag.outbox`
   already holds the `diag.investigation.recorded` row whose correlation ID is
   the key;
3. otherwise runs the existing `Investigations.investigate` as the named org
   member (IAM resolves the access, REPO's verifier the code-host standing),
   with the key as the `Origin` correlation ID, heartbeating meanwhile.

So a worker killed mid-activity still yields one recorded investigation; an
advisory-model call made before the kill may be repeated (at-least-once), and
its request row is recorded each time, as today. Reason codes: database
errors are `database_unavailable` (retryable); `not_found`, `unauthorized`,
`invalid_request` and `investigation_failed` are final.
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.exceptions import CancelledError as TemporalCancelledError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from flakepilot.diagnosis.application.assist import Assist
from flakepilot.diagnosis.application.investigation import (
    Investigation,
    InvestigationReport,
    StageStatus,
)
from flakepilot.diagnosis.infrastructure.tables import outbox as diag_outbox
from flakepilot.identity.application.authorization import (
    PermissionDenied,
    ResourceNotFound,
)
from flakepilot.identity.application.iam import Iam, SessionPrincipal, uuid_ok
from flakepilot.identity.application.verification import CodeHostAccessVerifier
from flakepilot.integrations.infrastructure.github import ISSUER as GITHUB_ISSUER
from flakepilot.interface.composition_root import (
    Settings,
    diag_unit_of_work,
    iam_unit_of_work,
    ti_unit_of_work,
)
from flakepilot.interface.composition_root.database import (
    create_engine,
    dispose_engine,
)
from flakepilot.interface.composition_root.investigations import Investigations
from flakepilot.interface.composition_root.providers import hosted_providers
from flakepilot.interface.composition_root.run_views import code_access_verifier
from flakepilot.interface.composition_root.temporal_workflow import (
    ACTIVITY,
    ID_REUSE,
    MAX_HEARTBEAT_SECONDS,
    TASK_QUEUE,
    TRANSIENT,
    WORKFLOW,
    InvestigationOutcome,
    InvestigationRequest,
    InvestigationWorkflow,
    workflow_id,
)
from flakepilot.shared_kernel.events import Channel, Origin
from flakepilot.test_intelligence.application.reliability import Reliability
from flakepilot.test_intelligence.application.run_view import RunViewer

_logger = logging.getLogger("flakepilot.temporal")

RECORDED = "diag.investigation.recorded"
_LOCK = "SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"

Investigate = Callable[[InvestigationRequest, Origin], Awaitable[InvestigationReport]]


def _refuse(reason: str) -> ApplicationError:
    return ApplicationError(reason, type=reason, non_retryable=reason not in TRANSIENT)


def _lock_key(key: str) -> str:
    return f"flakepilot.investigation:{key}"


def _request_ok(request: InvestigationRequest) -> bool:
    return (
        uuid_ok(request.key)
        and uuid_ok(request.org_id)
        and uuid_ok(request.run_id)
        and request.member_github_id.isdigit()
        and len(request.member_github_id) <= 20
    )


class InvestigationActivity:
    def __init__(
        self, engine: AsyncEngine, investigate: Investigate, heartbeat_every: float = 5
    ) -> None:
        self._engine = engine
        self._investigate = investigate
        self._heartbeat_every = heartbeat_every

    async def recorded(self, key: str) -> InvestigationOutcome | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        diag_outbox.c.after,
                        func.coalesce(
                            diag_outbox.c.payload["stages_produced"].as_integer(), 0
                        ).label("produced"),
                    )
                    .where(
                        diag_outbox.c.name == RECORDED,
                        diag_outbox.c.correlation_id == key,
                    )
                    .order_by(diag_outbox.c.seq)
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        return InvestigationOutcome(key, str(row.after), int(row.produced), True)

    @activity.defn(name=ACTIVITY)
    async def run(self, request: InvestigationRequest) -> InvestigationOutcome:
        if not _request_ok(request):
            raise _refuse("invalid_request")
        try:
            connection = await self._engine.connect()
        except (OSError, DBAPIError, OperationalError, TimeoutError):
            raise _refuse("database_unavailable") from None
        try:
            # Autocommit: the session lock must not depend on a transaction.
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            locked = await connection.scalar(
                text(_LOCK), {"key": _lock_key(request.key)}
            )
            if not locked:
                raise _refuse("investigation_in_progress")
            existing = await self.recorded(request.key)
            if existing is not None:
                return existing
            return await self._run_with_heartbeat(request)
        except (OSError, DBAPIError, OperationalError, TimeoutError):
            raise _refuse("database_unavailable") from None
        finally:
            # Closing the connection releases the session lock even when the
            # unlock statement itself could not run (cancellation, a dead socket).
            await connection.invalidate()
            await connection.close()

    async def _run_with_heartbeat(
        self, request: InvestigationRequest
    ) -> InvestigationOutcome:
        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(self._heartbeat_every)

        beating = asyncio.create_task(beat())
        try:
            report = await self._investigate(request, Origin(Channel.CLI, request.key))
        except ResourceNotFound:
            raise _refuse("not_found") from None
        except PermissionDenied:
            raise _refuse("unauthorized") from None
        except (ApplicationError, asyncio.CancelledError):
            raise
        except (OSError, DBAPIError, OperationalError, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001 - only the class is logged
            _logger.error("investigation failed: %s", type(exc).__name__)
            raise _refuse("investigation_failed") from None
        finally:
            beating.cancel()
        produced = sum(1 for s in report.stages if s.status is StageStatus.PRODUCED)
        return InvestigationOutcome(request.key, report.stopping.value, produced, False)


def member_investigation(
    engine: AsyncEngine,
    settings: Settings,
    verifier: CodeHostAccessVerifier | None = None,
) -> Investigate:
    """`Investigations.investigate` for an explicitly named org member, wired
    like the API process (hosted providers included, speech excluded)."""
    providers = hosted_providers(settings)
    iam = Iam(iam_unit_of_work(engine), identity_provider=None)
    diag, ti = diag_unit_of_work(engine), ti_unit_of_work(engine)
    investigations = Investigations(
        Investigation(
            runs=RunViewer(ti),
            assist=Assist(diag, providers.assist),
            reliability=Reliability(ti),
            unit_of_work=diag,
            research=providers.research,
            browser=providers.browser,
        ),
        ti,
        verifier or code_access_verifier(engine, settings),
        iam.login,
        None,
    )

    async def investigate(
        request: InvestigationRequest, origin: Origin
    ) -> InvestigationReport:
        user_id = await iam.find_user(
            issuer=GITHUB_ISSUER, subject=request.member_github_id
        )
        if user_id is None:
            raise ResourceNotFound
        access = await iam.org_access(SessionPrincipal(user_id), request.org_id)
        return await investigations.investigate(
            access=access, org_id=request.org_id, run_id=request.run_id, origin=origin
        )

    return investigate


def build_worker(client: Client, activity_impl: InvestigationActivity) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[InvestigationWorkflow],
        activities=[activity_impl.run],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "flakepilot"
            )
        ),
        max_concurrent_activities=1,
    )


async def connect(settings: Settings) -> Client:
    assert settings.temporal_address and settings.temporal_namespace
    api_key = settings.temporal_api_key
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=api_key.reveal() if api_key else None,
        tls=api_key is not None,
    )


async def start_investigation(
    client: Client, request: InvestigationRequest
) -> InvestigationOutcome:
    handle = await client.start_workflow(
        WORKFLOW,
        request,
        id=workflow_id(request.key),
        task_queue=TASK_QUEUE,
        id_reuse_policy=ID_REUSE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        result_type=InvestigationOutcome,
    )
    outcome: InvestigationOutcome = await handle.result()
    return outcome


def failure_reason(error: WorkflowFailureError) -> str:
    """The activity's reason code, `cancelled`, or the failure's class name."""
    cause: BaseException | None = error.cause
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type:
            return cause.type
        if isinstance(cause, TemporalCancelledError):
            return "cancelled"
        cause = cause.__cause__
    return type(error.cause).__name__


def _configured(settings: Settings, command: str) -> bool:
    if settings.temporal_address is None or settings.temporal_namespace is None:
        print(
            f"{command}: needs FLAKEPILOT_TEMPORAL_ADDRESS and "
            "FLAKEPILOT_TEMPORAL_NAMESPACE",
            file=sys.stderr,
        )
        return False
    return True


def run_temporal_worker(settings: Settings, argv: Sequence[str]) -> int:
    argparse.ArgumentParser(
        prog="temporal-worker",
        description="Run the opt-in Temporal investigation worker (goal 5 POC).",
    ).parse_args(argv)
    if not _configured(settings, "temporal-worker"):
        return 1

    async def main() -> None:
        engine = create_engine(settings.database_url.reveal())
        try:
            client = await connect(settings)
            impl = InvestigationActivity(engine, member_investigation(engine, settings))
            await build_worker(client, impl).run()
        finally:
            await dispose_engine(engine)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - errors may echo addresses or keys
        print(f"temporal-worker failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def _parse_run(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="temporal-run-investigation",
        description=(
            "Start one investigation workflow and wait for its outcome "
            "(goal 5 POC; FlakePilot's lock and outbox stay authoritative)."
        ),
    )
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--member-github-id", required=True, help="numeric GitHub ID of an org member"
    )
    parser.add_argument("--key", help="investigation key (a UUID); default: new")
    return parser.parse_args(argv)


def run_temporal_investigation(settings: Settings, argv: Sequence[str]) -> int:
    args = _parse_run(argv)
    request = InvestigationRequest(
        key=args.key or str(uuid.uuid4()),
        org_id=args.org_id,
        run_id=args.run_id,
        member_github_id=args.member_github_id,
        heartbeat_timeout_seconds=MAX_HEARTBEAT_SECONDS,
    )
    if not _request_ok(request):
        print(
            "temporal-run-investigation: --org-id, --run-id and --key must be "
            "UUIDs and --member-github-id numeric",
            file=sys.stderr,
        )
        return 1
    if not _configured(settings, "temporal-run-investigation"):
        return 1

    async def main() -> dict[str, Any]:
        client = await connect(settings)
        return asdict(await start_investigation(client, request))

    try:
        outcome = asyncio.run(main())
    except WorkflowFailureError as exc:
        print(f"temporal-run-investigation: {failure_reason(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - errors may echo addresses or keys
        print(
            f"temporal-run-investigation failed: {type(exc).__name__}", file=sys.stderr
        )
        return 1
    print(json.dumps(outcome, indent=2))
    return 0


def run_temporal_cancel(settings: Settings, argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="temporal-cancel-investigation",
        description="Cancel one investigation workflow (its activity stops too).",
    )
    parser.add_argument("--key", required=True, help="investigation key (a UUID)")
    args = parser.parse_args(argv)
    if not uuid_ok(args.key):
        print("temporal-cancel-investigation: --key must be a UUID", file=sys.stderr)
        return 1
    if not _configured(settings, "temporal-cancel-investigation"):
        return 1

    async def main() -> None:
        client = await connect(settings)
        await client.get_workflow_handle(workflow_id(args.key)).cancel()

    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 - errors may echo addresses or keys
        print(
            f"temporal-cancel-investigation failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print("cancel requested")
    return 0
