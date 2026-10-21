"""Deliver iam, ti, diag, and exec outbox rows to AUD. Polling is sufficient.

Each event is claimed with `FOR UPDATE SKIP LOCKED` and finished in that same
transaction: AUD's insert and `published_at` commit together, or neither does.
A handler outside this transaction (there is none today) could still run twice.
Invalid envelopes are parked on the outbox row (`dead_at`); they are not deleted.

PostgreSQL `LISTEN/NOTIFY` is not used. A missed, duplicate, or early wake-up cannot
change the result, because the next poll reads the table.
"""

import logging

from sqlalchemy import Table, case, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql import func

from flakepilot.audit.application.log import AuditRecord
from flakepilot.audit.infrastructure.store import SqlAuditStore
from flakepilot.diagnosis.infrastructure.tables import outbox as diag_outbox
from flakepilot.execution.infrastructure.tables import outbox as exec_outbox
from flakepilot.identity.infrastructure.tables import outbox as iam_outbox
from flakepilot.interface.composition_root.database import transaction
from flakepilot.shared_kernel.events import (
    SCHEMA_VERSION,
    Actor,
    ActorType,
    Channel,
    Event,
    Origin,
    Subject,
)
from flakepilot.test_intelligence.infrastructure.tables import outbox as ti_outbox

_logger = logging.getLogger("flakepilot.worker")

SOURCES = (
    ("iam", iam_outbox),
    ("ti", ti_outbox),
    ("diag", diag_outbox),
    ("exec", exec_outbox),
)


async def deliver_batch(engine: AsyncEngine, limit: int) -> int:
    """Publish up to `limit` rows from each outbox. Returns how many rows were finished."""
    finished = 0
    for source, table in SOURCES:
        for _ in range(limit):
            async with transaction(engine) as session:
                row = await claim_one(session, table)
                if row is None:
                    break
                await publish_one(session, source, table, row)
            finished += 1
    return finished


async def claim_one(session: AsyncSession, table: Table) -> RowMapping | None:
    return (
        (
            await session.execute(
                select(table)
                .where(table.c.published_at.is_(None), table.c.dead_at.is_(None))
                .order_by(table.c.seq)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        )
        .mappings()
        .first()
    )


async def publish_one(
    session: AsyncSession, source: str, table: Table, row: RowMapping
) -> None:
    record = to_record(source, row)
    if record is None:
        await session.execute(
            update(table)
            .where(table.c.event_id == row["event_id"])
            .values(
                dead_at=func.now(),
                last_error="invalid_envelope",
                attempts=case(
                    (table.c.attempts < 20, table.c.attempts + 1),
                    else_=table.c.attempts,
                ),
            )
        )
        _logger.error("outbox dead: %s invalid_envelope %s", source, row["event_id"])
        return
    await SqlAuditStore(session).append(record)
    await session.execute(
        update(table)
        .where(table.c.event_id == row["event_id"])
        .values(published_at=func.now())
    )
    _logger.info("outbox published: %s %s", source, row["event_id"])


def to_record(source: str, row: RowMapping) -> AuditRecord | None:
    event = _event(row)
    if event is None:
        return None
    occurred_at = row["occurred_at"]
    return AuditRecord(
        event_id=event.event_id,
        source=source,
        name=event.name,
        schema_version=event.schema_version,
        occurred_at=occurred_at,
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


def _event(row: RowMapping) -> Event | None:
    payload = row["payload"]
    if type(payload) is not dict or row["schema_version"] != SCHEMA_VERSION:
        return None
    try:
        return Event(
            name=str(row["name"]),
            actor=Actor(ActorType(str(row["actor_type"])), _text(row["actor_id"])),
            origin=Origin(Channel(str(row["channel"])), str(row["correlation_id"])),
            subject=Subject(str(row["subject_type"]), str(row["subject_id"])),
            org_id=_text(row["org_id"]),
            repo_id=_text(row["repo_id"]),
            before=_text(row["before"]),
            after=_text(row["after"]),
            reason=_text(row["reason"]),
            payload=payload,
            event_id=str(row["event_id"]),
            schema_version=SCHEMA_VERSION,
        )
    except (ValueError, TypeError):
        return None


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
