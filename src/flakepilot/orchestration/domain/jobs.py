"""Durable job rules (ADR-006). Pure values: no I/O and no clock read.

Payloads are IDs and enum tokens. They are never code, imports, or free text.
"""

import json
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from datetime import timedelta

SCHEDULED = "scheduled"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

PAYLOAD_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 5
MAX_ATTEMPTS = 20
MAX_BACKOFF_SECONDS = 60
MAX_PAYLOAD_BYTES = 2048
MAX_BATCH = 100

_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,100}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_OWNER = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class InvalidJob(ValueError):
    """The job was rejected before any row was written. `reason` is a short code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def error_code(reason: str) -> str:
    """A stored error is a short code, never a message that might carry data."""
    if _CODE.fullmatch(reason):
        return reason
    return "invalid_error"


def owner_ok(owner: str) -> bool:
    return _OWNER.fullmatch(owner) is not None


def reference_ok(value: str | None) -> bool:
    return value is None or _TOKEN.fullmatch(value) is not None


def backoff(attempts: int) -> timedelta:
    """Bounded exponential delay after `attempts` claims. Attempt 1 waits 1s, then 2, 4, …, 60."""
    power = min(max(attempts, 1) - 1, 6)
    return timedelta(seconds=min(MAX_BACKOFF_SECONDS, 2**power))


def gives_up(attempts: int, max_attempts: int, *, permanent: bool) -> bool:
    return permanent or attempts >= max_attempts


def validate_job(
    job_type: str,
    payload: Mapping[str, object],
    *,
    allowed: AbstractSet[str],
    version: int = PAYLOAD_VERSION,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Raise `InvalidJob` when the type is unregistered or the payload is not data."""
    if job_type not in allowed or _TYPE.fullmatch(job_type) is None:
        raise InvalidJob("unknown_type")
    if version != PAYLOAD_VERSION:
        raise InvalidJob("unsupported_version")
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise InvalidJob("invalid_payload")
    if type(payload) is not dict or len(payload) > 20:
        raise InvalidJob("invalid_payload")
    for key, value in payload.items():
        if type(key) is not str or _KEY.fullmatch(key) is None or not _value_ok(value):
            raise InvalidJob("invalid_payload")
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise InvalidJob("invalid_payload")


def _value_ok(value: object) -> bool:
    if value is None or type(value) is bool:
        return True
    if type(value) is str:
        return _TOKEN.fullmatch(value) is not None
    if type(value) is int:
        return -1_000_000_000 <= value <= 1_000_000_000
    return False
