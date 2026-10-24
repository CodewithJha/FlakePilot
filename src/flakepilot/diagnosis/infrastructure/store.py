"""Diagnosis PostgreSQL store. Runs inside a caller's transaction and never commits."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.diagnosis.application.store import (
    IN_FLIGHT,
    AiPolicy,
    AiRequestRecord,
)
from flakepilot.diagnosis.domain.output import DataPolicy
from flakepilot.diagnosis.infrastructure.tables import ai_policies, ai_requests, outbox
from flakepilot.shared_kernel.events import Event

_DEFAULT = AiPolicy(DataPolicy.DISABLED, False)


class SqlDiagStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def policy(self, org_id: str) -> AiPolicy:
        row = (
            await self._session.execute(
                select(ai_policies.c.level, ai_policies.c.retain_raw).where(
                    ai_policies.c.org_id == org_id
                )
            )
        ).one_or_none()
        if row is None:
            return _DEFAULT
        return AiPolicy(DataPolicy(row.level), row.retain_raw)

    async def set_policy(self, org_id: str, policy: AiPolicy) -> None:
        statement = insert(ai_policies).values(
            org_id=org_id,
            level=policy.level.value,
            retain_raw=policy.retain_raw,
            updated_at=func.now(),
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[ai_policies.c.org_id],
                set_={
                    "level": policy.level.value,
                    "retain_raw": policy.retain_raw,
                    "updated_at": func.now(),
                },
            )
        )

    async def record_request(self, request: AiRequestRecord) -> None:
        await self._session.execute(
            ai_requests.insert().values(
                id=request.id,
                org_id=request.org_id,
                repo_id=request.repo_id,
                actor_id=request.actor_id,
                policy_level=request.policy_level.value,
                retain_raw=request.retain_raw,
                provider=request.provider,
                model=request.model,
                payload_sha256=request.payload_sha256,
                payload_size=request.payload_size,
                outcome=request.outcome.value,
                validation_reason=request.validation_reason,
                linked_count=request.linked_count,
                unverified_count=request.unverified_count,
                contradicted_count=request.contradicted_count,
                raw_output=request.raw_output,
                created_at=func.now(),
            )
        )

    async def finish_request(self, request: AiRequestRecord) -> None:
        statement = (
            ai_requests.update()
            .where(
                ai_requests.c.id == request.id,
                ai_requests.c.org_id == request.org_id,
                ai_requests.c.validation_reason == IN_FLIGHT,
            )
            .values(
                provider=request.provider,
                model=request.model,
                outcome=request.outcome.value,
                validation_reason=request.validation_reason,
                linked_count=request.linked_count,
                unverified_count=request.unverified_count,
                contradicted_count=request.contradicted_count,
                raw_output=request.raw_output,
            )
            .returning(ai_requests.c.id)
        )
        (await self._session.execute(statement)).one()

    async def append_event(self, event: Event) -> None:
        await self._session.execute(
            outbox.insert().values(
                event_id=event.event_id,
                name=event.name,
                schema_version=event.schema_version,
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
