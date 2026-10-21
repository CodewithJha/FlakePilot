"""Job values and the store port. No SQL and no handler imports.

Callers pass an aware clock. The store compares leases to that instant, so tests move
time by passing a later value instead of sleeping.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol

from flakepilot.orchestration.domain.jobs import (
    DEFAULT_MAX_ATTEMPTS,
)

Payload = Mapping[str, str | int | bool | None]


class JobError(Exception):
    """A handler outcome. `reason` is a short code, not a message."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RetryableJob(JobError):
    """Try again until the attempt bound, then stop in `failed`."""


class PermanentJob(JobError):
    """Stop in `failed` without using the remaining attempts."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class Job:
    """One durable job. `payload` is data the handler already trusts as IDs and tokens."""

    __slots__ = (
        "attempts",
        "cancel_requested",
        "causation_id",
        "correlation_id",
        "id",
        "job_type",
        "last_error",
        "lease_expires_at",
        "lease_owner",
        "max_attempts",
        "payload",
        "payload_version",
        "run_at",
        "state",
    )

    def __init__(
        self,
        *,
        id: str,
        job_type: str,
        payload_version: int,
        payload: Payload,
        state: str,
        run_at: datetime,
        attempts: int,
        max_attempts: int,
        lease_owner: str | None,
        lease_expires_at: datetime | None,
        last_error: str | None,
        correlation_id: str | None,
        causation_id: str | None,
        cancel_requested: bool,
    ) -> None:
        self.id = id
        self.job_type = job_type
        self.payload_version = payload_version
        self.payload = payload
        self.state = state
        self.run_at = run_at
        self.attempts = attempts
        self.max_attempts = max_attempts
        self.lease_owner = lease_owner
        self.lease_expires_at = lease_expires_at
        self.last_error = last_error
        self.correlation_id = correlation_id
        self.causation_id = causation_id
        self.cancel_requested = cancel_requested


class JobStore(Protocol):
    """Claim and settle jobs inside the caller's transaction. Never commits."""

    async def enqueue(
        self,
        *,
        job_type: str,
        payload: Mapping[str, object],
        now: datetime,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        run_at: datetime | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> str: ...

    async def claim(
        self, *, owner: str, now: datetime, limit: int, lease: timedelta
    ) -> list[Job]: ...

    async def complete(self, *, job_id: str, owner: str, now: datetime) -> bool: ...

    async def renew(
        self, *, job_id: str, owner: str, now: datetime, lease: timedelta
    ) -> bool: ...

    async def fail(
        self,
        *,
        job_id: str,
        owner: str,
        now: datetime,
        error: str,
        permanent: bool,
    ) -> bool: ...

    async def request_cancel(self, *, job_id: str, now: datetime) -> bool: ...

    async def acknowledge_cancel(
        self, *, job_id: str, owner: str, now: datetime
    ) -> bool: ...

    async def get(self, job_id: str) -> Job | None: ...
