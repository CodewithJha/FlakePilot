"""ORCH job store. Runs inside a caller's transaction and never commits.

Lease instants are the caller's clock, passed in as parameters, so two workers agree
only as closely as their clocks do. Tests advance that clock instead of sleeping.
"""

import uuid
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from flakepilot.orchestration.application.jobs import Job
from flakepilot.orchestration.domain.jobs import (
    CANCELLED,
    COMPLETED,
    FAILED,
    MAX_BATCH,
    RUNNING,
    SCHEDULED,
    InvalidJob,
    backoff,
    error_code,
    gives_up,
    owner_ok,
    reference_ok,
    validate_job,
)
from flakepilot.orchestration.infrastructure.tables import jobs


class SqlJobStore:
    def __init__(self, session: AsyncSession, allowed: AbstractSet[str]) -> None:
        self._session = session
        self._allowed = allowed

    async def enqueue(
        self,
        *,
        job_type: str,
        payload: Mapping[str, object],
        now: datetime,
        max_attempts: int = 5,
        run_at: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> str:
        _aware(now)
        validate_job(
            job_type,
            payload,
            allowed=self._allowed,
            max_attempts=max_attempts,
        )
        if not reference_ok(correlation_id) or not reference_ok(causation_id):
            raise InvalidJob("invalid_payload")
        when = run_at if run_at is not None else now
        _aware(when)
        job_id = str(uuid.uuid4())
        await self._session.execute(
            jobs.insert().values(
                id=job_id,
                job_type=job_type,
                payload_version=1,
                payload=dict(payload),
                state=SCHEDULED,
                run_at=when,
                attempts=0,
                max_attempts=max_attempts,
                created_at=now,
                updated_at=now,
                correlation_id=correlation_id,
                causation_id=causation_id,
                cancel_requested=False,
            )
        )
        return job_id

    async def claim(
        self, *, owner: str, now: datetime, limit: int, lease: timedelta
    ) -> list[Job]:
        _aware(now)
        if not owner_ok(owner) or not 1 <= limit <= MAX_BATCH or lease <= timedelta(0):
            raise InvalidJob("invalid_payload")
        due = or_(
            and_(jobs.c.state == SCHEDULED, jobs.c.run_at <= now),
            and_(jobs.c.state == RUNNING, jobs.c.lease_expires_at <= now),
        )
        selected = (
            (
                await self._session.execute(
                    select(jobs)
                    .where(due)
                    .order_by(jobs.c.run_at, jobs.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        claimed: list[Job] = []
        for row in selected:
            if row["cancel_requested"] or not _stored_ok(row, self._allowed):
                await self._terminal(
                    row["id"],
                    now,
                    CANCELLED if row["cancel_requested"] else FAILED,
                    None
                    if row["cancel_requested"]
                    else _stored_error(row, self._allowed),
                )
                continue
            if row["state"] == RUNNING and row["attempts"] >= row["max_attempts"]:
                await self._terminal(row["id"], now, FAILED, "exhausted")
                continue
            attempts = int(row["attempts"]) + 1
            expires = now + lease
            await self._session.execute(
                update(jobs)
                .where(jobs.c.id == row["id"])
                .values(
                    state=RUNNING,
                    attempts=attempts,
                    lease_owner=owner,
                    lease_expires_at=expires,
                    updated_at=now,
                )
            )
            claimed.append(_job(row, attempts=attempts, owner=owner, expires=expires))
        return claimed

    async def complete(self, *, job_id: str, owner: str, now: datetime) -> bool:
        row = await self._lock_held(job_id, owner, now)
        if row is None or row["cancel_requested"]:
            return False
        await self._terminal(job_id, now, COMPLETED, None)
        return True

    async def renew(
        self, *, job_id: str, owner: str, now: datetime, lease: timedelta
    ) -> bool:
        _aware(now)
        if lease <= timedelta(0):
            raise InvalidJob("invalid_payload")
        row = await self._lock_held(job_id, owner, now)
        if row is None:
            return False
        updated = await self._session.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(lease_expires_at=now + lease, updated_at=now)
            .returning(jobs.c.id)
        )
        return updated.first() is not None

    async def fail(
        self,
        *,
        job_id: str,
        owner: str,
        now: datetime,
        error: str,
        permanent: bool,
    ) -> bool:
        row = await self._lock_held(job_id, owner, now)
        if row is None:
            return False
        code = error_code(error)
        if gives_up(
            int(row["attempts"]), int(row["max_attempts"]), permanent=permanent
        ):
            await self._terminal(job_id, now, FAILED, code)
            return True
        await self._session.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                state=SCHEDULED,
                run_at=now + backoff(int(row["attempts"])),
                lease_owner=None,
                lease_expires_at=None,
                last_error=code,
                updated_at=now,
            )
        )
        return True

    async def request_cancel(self, *, job_id: str, now: datetime) -> bool:
        _aware(now)
        updated = await self._session.execute(
            update(jobs)
            .where(jobs.c.id == job_id, jobs.c.state.in_((SCHEDULED, RUNNING)))
            .values(cancel_requested=True, updated_at=now)
            .returning(jobs.c.id)
        )
        return updated.first() is not None

    async def acknowledge_cancel(
        self, *, job_id: str, owner: str, now: datetime
    ) -> bool:
        row = await self._lock_held(job_id, owner, now)
        if row is None or not row["cancel_requested"]:
            return False
        await self._terminal(job_id, now, CANCELLED, None)
        return True

    async def get(self, job_id: str) -> Job | None:
        row = (
            (await self._session.execute(select(jobs).where(jobs.c.id == job_id)))
            .mappings()
            .first()
        )
        if row is None:
            return None
        return _job(
            row,
            attempts=int(row["attempts"]),
            owner=row["lease_owner"],
            expires=row["lease_expires_at"],
            state=str(row["state"]),
        )

    async def _lock_held(
        self, job_id: str, owner: str, now: datetime
    ) -> RowMapping | None:
        _aware(now)
        if not owner_ok(owner):
            return None
        return (
            (
                await self._session.execute(
                    select(jobs)
                    .where(
                        jobs.c.id == job_id,
                        jobs.c.state == RUNNING,
                        jobs.c.lease_owner == owner,
                        jobs.c.lease_expires_at > now,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )

    async def _terminal(
        self, job_id: str, now: datetime, state: str, error: str | None
    ) -> None:
        await self._session.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                state=state,
                lease_owner=None,
                lease_expires_at=None,
                last_error=error,
                updated_at=now,
            )
        )


def _aware(now: datetime) -> None:
    if now.tzinfo is None:
        raise InvalidJob("invalid_payload")


def _stored_ok(row: RowMapping, allowed: AbstractSet[str]) -> bool:
    payload = _payload(row["payload"])
    if payload is None:
        return False
    try:
        validate_job(
            str(row["job_type"]),
            payload,
            allowed=allowed,
            version=int(row["payload_version"]),
            max_attempts=int(row["max_attempts"]),
        )
    except InvalidJob:
        return False
    return True


def _stored_error(row: RowMapping, allowed: AbstractSet[str]) -> str:
    payload = _payload(row["payload"])
    if payload is None:
        return "invalid_payload"
    try:
        validate_job(
            str(row["job_type"]),
            payload,
            allowed=allowed,
            version=int(row["payload_version"]),
            max_attempts=int(row["max_attempts"]),
        )
    except InvalidJob as exc:
        return exc.reason
    return "invalid_payload"


def _payload(value: object) -> dict[str, str | int | bool | None] | None:
    if type(value) is not dict:
        return None
    cleaned: dict[str, str | int | bool | None] = {}
    for key, item in value.items():
        if type(key) is not str:
            return None
        if item is None or type(item) is bool or type(item) is str or type(item) is int:
            cleaned[key] = item
            continue
        return None
    return cleaned


def _job(
    row: RowMapping,
    *,
    attempts: int,
    owner: str | None,
    expires: datetime | None,
    state: str = RUNNING,
) -> Job:
    payload = _payload(row["payload"])
    return Job(
        id=str(row["id"]),
        job_type=str(row["job_type"]),
        payload_version=int(row["payload_version"]),
        payload=payload if payload is not None else {},
        state=state,
        run_at=row["run_at"],
        attempts=attempts,
        max_attempts=int(row["max_attempts"]),
        lease_owner=owner,
        lease_expires_at=expires,
        last_error=row["last_error"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        cancel_requested=bool(row["cancel_requested"]),
    )
