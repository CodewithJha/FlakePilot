"""Wake a scheduled execution with a Stage 6 job. The payload is the id only.

The execution insert and the job insert are different domains, so they are
different transactions. A crash between them is repaired by
`enqueue_missing`, which the worker runs before polling.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from flakepilot.execution.application.reproduction import JOB_TYPE
from flakepilot.execution.infrastructure.store import SqlExecutionStore
from flakepilot.interface.composition_root.database import transaction
from flakepilot.orchestration.infrastructure.store import SqlJobStore

_ALLOWED = frozenset({JOB_TYPE})


async def enqueue_browser_job(
    engine: AsyncEngine, execution_id: str, now: datetime
) -> str | None:
    async with transaction(engine) as session:
        current = await SqlExecutionStore(session).get(execution_id)
        if (
            current is None
            or current.state != "scheduled"
            or current.job_id is not None
        ):
            return None
        correlation_id = current.correlation_id
    async with transaction(engine) as session:
        job_id = await SqlJobStore(session, _ALLOWED).enqueue(
            job_type=JOB_TYPE,
            payload={"execution_id": execution_id},
            now=now,
            correlation_id=correlation_id,
        )
    async with transaction(engine) as session:
        attached = await SqlExecutionStore(session).attach_job(
            execution_id=execution_id, job_id=job_id
        )
    return job_id if attached else None


async def enqueue_missing(engine: AsyncEngine, now: datetime, limit: int = 100) -> int:
    async with transaction(engine) as session:
        pending = await SqlExecutionStore(session).scheduled_without_job(limit)
    queued = 0
    for execution_id in pending:
        if await enqueue_browser_job(engine, execution_id, now) is not None:
            queued += 1
    return queued
