"""Append-only audit store. Runs inside a caller's transaction and never commits.

A second insert of the same event id is a no-op (`ON CONFLICT DO NOTHING`). There is
no update and no delete.
"""

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.audit.application.log import AuditRecord
from flakepilot.audit.infrastructure.tables import events


class SqlAuditStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, record: AuditRecord) -> bool:
        inserted = await self._session.execute(
            insert(events)
            .values(
                event_id=record.event_id,
                source=record.source,
                name=record.name,
                schema_version=record.schema_version,
                occurred_at=record.occurred_at,
                org_id=record.org_id,
                repo_id=record.repo_id,
                actor_type=record.actor_type,
                actor_id=record.actor_id,
                channel=record.channel,
                correlation_id=record.correlation_id,
                subject_type=record.subject_type,
                subject_id=record.subject_id,
                before=record.before,
                after=record.after,
                reason=record.reason,
                payload=dict(record.payload),
                recorded_at=func.now(),
            )
            .on_conflict_do_nothing(index_elements=[events.c.event_id])
            .returning(events.c.event_id)
        )
        return inserted.first() is not None
