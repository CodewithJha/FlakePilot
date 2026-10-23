"""Registration persistence. Runs inside the caller's transaction and never commits.

Idempotency is explicit: consult the ID first, then the (host, owner, name)
coordinates, and only insert when both are free. Unique constraints alone would leave a
conflict outcome indistinguishable from a duplicate; consulting first also lets the
exact repeat return without an event.
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.repository.application.registration import (
    OUTCOME_ALREADY_REGISTERED,
    OUTCOME_REGISTERED,
    RegistrationConflict,
)
from flakepilot.repository.application.source import RepoBinding
from flakepilot.repository.infrastructure.tables import outbox, repositories
from flakepilot.shared_kernel.events import Event

_MESSAGE_BY_WHICH = "the repository ID is registered with different coordinates"
_MESSAGE_BY_COORDINATES = "the repository is already registered under a different ID"


class SqlRegistrationStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, binding: RepoBinding, *, now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("invalid_request")
        by_id = await self._session.execute(
            select(
                repositories.c.org_id, repositories.c.owner, repositories.c.name
            ).where(repositories.c.id == binding.repo_id)
        )
        found = by_id.first()
        if found is not None:
            same = (
                str(found.org_id) == binding.org_id
                and found.owner == binding.owner
                and found.name == binding.name
            )
            if same:
                return OUTCOME_ALREADY_REGISTERED
            raise RegistrationConflict(_MESSAGE_BY_WHICH)
        coordinates = await self._session.execute(
            select(repositories.c.id).where(
                repositories.c.host == "github",
                repositories.c.owner == binding.owner,
                repositories.c.name == binding.name,
            )
        )
        other = coordinates.first()
        if other is not None:
            raise RegistrationConflict(_MESSAGE_BY_COORDINATES)
        await self._session.execute(
            repositories.insert().values(
                id=binding.repo_id,
                org_id=binding.org_id,
                host="github",
                owner=binding.owner,
                name=binding.name,
                created_at=now,
            )
        )
        return OUTCOME_REGISTERED

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
