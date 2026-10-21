"""An audit record is one stored envelope. No I/O."""

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

Payload = Mapping[str, str | int | bool | None]


class AuditRecord:
    """The envelope as AUD stores it, plus which outbox it was copied from."""

    __slots__ = (
        "actor_id",
        "actor_type",
        "after",
        "before",
        "channel",
        "correlation_id",
        "event_id",
        "name",
        "occurred_at",
        "org_id",
        "payload",
        "reason",
        "repo_id",
        "schema_version",
        "source",
        "subject_id",
        "subject_type",
    )

    def __init__(
        self,
        *,
        event_id: str,
        source: str,
        name: str,
        schema_version: int,
        occurred_at: datetime,
        org_id: str | None,
        repo_id: str | None,
        actor_type: str,
        actor_id: str | None,
        channel: str,
        correlation_id: str,
        subject_type: str,
        subject_id: str,
        before: str | None,
        after: str | None,
        reason: str | None,
        payload: Payload,
    ) -> None:
        self.event_id = event_id
        self.source = source
        self.name = name
        self.schema_version = schema_version
        self.occurred_at = occurred_at
        self.org_id = org_id
        self.repo_id = repo_id
        self.actor_type = actor_type
        self.actor_id = actor_id
        self.channel = channel
        self.correlation_id = correlation_id
        self.subject_type = subject_type
        self.subject_id = subject_id
        self.before = before
        self.after = after
        self.reason = reason
        self.payload = payload


class AuditLog(Protocol):
    """Insert one record. A repeat of the same event id inserts nothing."""

    async def append(self, record: AuditRecord) -> bool: ...
