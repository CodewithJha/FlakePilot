"""Execution records. Runs inside the caller's transaction and never commits.

Claim and finish compare the lease to the caller's clock. A finish whose
generation or owner does not match writes nothing, including no outbox row.
"""

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from flakepilot.execution.application.reproduction import Execution, InvalidExecution
from flakepilot.execution.domain.reproduction import (
    CANCELLED,
    COMPLETED,
    RUNNING,
    SCHEDULED,
    Decision,
)
from flakepilot.execution.infrastructure.tables import (
    execution_artifacts,
    executions,
    outbox,
)
from flakepilot.shared_kernel.events import (
    SCHEMA_VERSION,
    Actor,
    ActorType,
    Channel,
    Event,
    Origin,
    Subject,
)

_OWNER = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_MAX_GENERATION = 20


class SqlExecutionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active(
        self, *, org_id: str, repo_id: str, selector: str, sha: str
    ) -> Execution | None:
        found = await self._session.execute(
            select(executions).where(
                executions.c.org_id == org_id,
                executions.c.repo_id == repo_id,
                executions.c.selector == selector,
                executions.c.sha == sha,
                or_(
                    executions.c.state == SCHEDULED,
                    executions.c.state == RUNNING,
                ),
            )
        )
        row = found.mappings().first()
        return None if row is None else Execution(dict(row))

    async def get(self, execution_id: str) -> Execution | None:
        if not _uuid(execution_id):
            return None
        found = await self._session.execute(
            select(executions).where(executions.c.id == execution_id)
        )
        row = found.mappings().first()
        return None if row is None else Execution(dict(row))

    async def insert(
        self,
        *,
        org_id: str,
        repo_id: str,
        selector: str,
        sha: str,
        decision: Decision,
        actor: Actor,
        origin: Origin,
        now: datetime,
    ) -> Execution:
        _clock(now)
        execution_id = str(uuid.uuid4())
        generation = 0 if decision.state == SCHEDULED else 1
        values = _row(
            execution_id,
            org_id=org_id,
            repo_id=repo_id,
            selector=selector,
            sha=sha,
            decision=decision,
            generation=generation,
            actor=actor,
            origin=origin,
            now=now,
        )
        try:
            async with self._session.begin_nested():
                await self._session.execute(executions.insert().values(**values))
                await self._event(
                    name="execution.reproduction.scheduled"
                    if decision.state == SCHEDULED
                    else "execution.reproduction.finished",
                    execution_id=execution_id,
                    org_id=org_id,
                    repo_id=repo_id,
                    actor=actor,
                    origin=origin,
                    before=None,
                    after=decision.outcome or decision.state,
                    reason=decision.reason,
                    generation=generation,
                )
        except IntegrityError:
            found = await self.find_active(
                org_id=org_id, repo_id=repo_id, selector=selector, sha=sha
            )
            if found is None:
                raise
            return found
        loaded = await self.get(execution_id)
        if loaded is None:
            raise InvalidExecution("invalid_request")
        return loaded

    async def claim(
        self,
        *,
        execution_id: str,
        owner: str,
        now: datetime,
        lease: timedelta,
    ) -> Execution | None:
        _clock(now)
        if not _OWNER.fullmatch(owner) or lease.total_seconds() <= 0:
            raise InvalidExecution("invalid_request")
        found = await self._session.execute(
            select(executions)
            .where(executions.c.id == execution_id)
            .with_for_update(skip_locked=True)
        )
        row = found.mappings().first()
        if row is None:
            return None
        current = Execution(dict(row))
        deadline = current.lease_expires_at
        expired = (
            current.state == RUNNING
            and isinstance(deadline, datetime)
            and deadline <= now
        )
        if current.state != SCHEDULED and not expired:
            return None
        if current.cancel_requested and (current.state == SCHEDULED or expired):
            await self._terminal(
                current,
                Decision(CANCELLED, CANCELLED, "cancelled"),
                now=now,
            )
            return None
        if current.generation >= _MAX_GENERATION:
            await self._terminal(
                current,
                Decision(COMPLETED, "infra_failure", "exhausted"),
                now=now,
            )
            return None
        generation = current.generation + 1
        expires = now + lease
        await self._session.execute(
            update(executions)
            .where(executions.c.id == execution_id)
            .values(
                state=RUNNING,
                generation=generation,
                lease_owner=owner,
                lease_expires_at=expires,
                updated_at=now,
            )
        )
        loaded = await self.get(execution_id)
        return loaded

    async def finish(
        self,
        *,
        execution_id: str,
        owner: str,
        generation: int,
        now: datetime,
        decision: Decision,
        evidence_partial: bool,
        actor: Actor,
        origin: Origin,
    ) -> bool:
        _clock(now)
        if decision.state not in (COMPLETED, CANCELLED):
            raise InvalidExecution("invalid_request")
        result = await self._session.execute(
            update(executions)
            .where(
                executions.c.id == execution_id,
                executions.c.state == RUNNING,
                executions.c.lease_owner == owner,
                executions.c.generation == generation,
                executions.c.lease_expires_at > now,
            )
            .values(
                state=decision.state,
                outcome=decision.outcome,
                reason=decision.reason,
                evidence_partial=evidence_partial,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
            .returning(executions.c.org_id, executions.c.repo_id)
        )
        row = result.first()
        if row is None:
            return False
        await self._event(
            name="execution.reproduction.finished",
            execution_id=execution_id,
            org_id=str(row.org_id),
            repo_id=str(row.repo_id),
            actor=actor,
            origin=origin,
            before=RUNNING,
            after=decision.outcome,
            reason=decision.reason,
            generation=generation,
        )
        return True

    async def request_cancel(self, *, execution_id: str, now: datetime) -> bool:
        _clock(now)
        result = await self._session.execute(
            update(executions)
            .where(
                executions.c.id == execution_id,
                or_(
                    executions.c.state == SCHEDULED,
                    executions.c.state == RUNNING,
                ),
            )
            .values(cancel_requested=True, updated_at=now)
            .returning(executions.c.id)
        )
        return result.first() is not None

    async def renew_lease(
        self,
        *,
        execution_id: str,
        owner: str,
        generation: int,
        now: datetime,
        lease: timedelta,
    ) -> bool:
        """Extend the lease of the current attempt. A stale owner or generation changes nothing."""
        _clock(now)
        if not _OWNER.fullmatch(owner) or lease.total_seconds() <= 0:
            raise InvalidExecution("invalid_request")
        result = await self._session.execute(
            update(executions)
            .where(
                executions.c.id == execution_id,
                executions.c.state == RUNNING,
                executions.c.lease_owner == owner,
                executions.c.generation == generation,
                executions.c.lease_expires_at > now,
            )
            .values(lease_expires_at=now + lease, updated_at=now)
            .returning(executions.c.id)
        )
        return result.first() is not None

    async def attach_job(self, *, execution_id: str, job_id: str) -> bool:
        if not (_uuid(execution_id) and _uuid(job_id)):
            raise InvalidExecution("invalid_request")
        result = await self._session.execute(
            update(executions)
            .where(
                executions.c.id == execution_id,
                executions.c.state == SCHEDULED,
                executions.c.job_id.is_(None),
            )
            .values(job_id=job_id)
            .returning(executions.c.id)
        )
        return result.first() is not None

    async def add_artifact(
        self,
        *,
        execution_id: str,
        kind: str,
        digest: bytes,
        size: int,
        artifact_key: str,
    ) -> None:
        if kind not in ("result", "screenshot") or len(digest) != 32:
            raise InvalidExecution("invalid_request")
        await self._session.execute(
            execution_artifacts.insert().values(
                id=str(uuid.uuid4()),
                execution_id=execution_id,
                kind=kind,
                sha256=digest,
                size=size,
                artifact_key=artifact_key,
            )
        )

    async def scheduled_without_job(self, limit: int) -> list[str]:
        if not 1 <= limit <= 100:
            raise InvalidExecution("invalid_request")
        found = await self._session.execute(
            select(executions.c.id)
            .where(
                executions.c.state == SCHEDULED,
                executions.c.job_id.is_(None),
            )
            .limit(limit)
        )
        return [str(row.id) for row in found]

    async def _terminal(
        self,
        current: Execution,
        decision: Decision,
        *,
        now: datetime,
    ) -> None:
        await self._session.execute(
            update(executions)
            .where(executions.c.id == current.id)
            .values(
                state=decision.state,
                outcome=decision.outcome,
                reason=decision.reason,
                evidence_partial=False,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        await self._event(
            name="execution.reproduction.finished",
            execution_id=current.id,
            org_id=current.org_id,
            repo_id=current.repo_id,
            actor=Actor(ActorType.SYSTEM),
            origin=Origin(Channel.CLI, current.correlation_id),
            before=current.state,
            after=decision.outcome,
            reason=decision.reason,
            generation=current.generation,
        )

    async def _event(
        self,
        *,
        name: str,
        execution_id: str,
        org_id: str,
        repo_id: str,
        actor: Actor,
        origin: Origin,
        before: str | None,
        after: str | None,
        reason: str | None,
        generation: int,
    ) -> None:
        event = Event(
            name=name,
            actor=actor,
            origin=origin,
            subject=Subject("execution", execution_id),
            org_id=org_id,
            repo_id=repo_id,
            before=before,
            after=after,
            reason=reason,
            payload={"generation": generation},
        )
        await self._session.execute(
            outbox.insert().values(
                event_id=event.event_id,
                name=event.name,
                schema_version=SCHEMA_VERSION,
                occurred_at=func.now(),
                org_id=event.org_id,
                repo_id=event.repo_id,
                actor_type=event.actor.type.value,
                actor_id=event.actor.id,
                channel=event.origin.channel.value,
                correlation_id=event.origin.correlation_id,
                subject_type=event.subject.type,
                subject_id=event.subject.id,
                before=event.before,
                after=event.after,
                reason=event.reason,
                payload=dict(event.payload),
            )
        )


def _clock(now: datetime) -> None:
    if now.tzinfo is None:
        raise InvalidExecution("invalid_request")


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _row(
    execution_id: str,
    *,
    org_id: str,
    repo_id: str,
    selector: str,
    sha: str,
    decision: Decision,
    generation: int,
    actor: Actor,
    origin: Origin,
    now: datetime,
) -> dict[str, object]:
    return {
        "id": execution_id,
        "org_id": org_id,
        "repo_id": repo_id,
        "selector": selector,
        "sha": sha,
        "state": decision.state,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "generation": generation,
        "lease_owner": None,
        "lease_expires_at": None,
        "job_id": None,
        "evidence_partial": False,
        "cancel_requested": False,
        "actor_type": actor.type.value,
        "actor_id": actor.id,
        "channel": origin.channel.value,
        "correlation_id": origin.correlation_id,
        "created_at": now,
        "updated_at": now,
    }
