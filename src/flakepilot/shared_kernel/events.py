"""Event envelope (ADR-012; SYSTEM-ARCHITECTURE §5). Pure values: no I/O, no clock.

Every state change appends one envelope to its context's outbox in the same transaction.
`occurred_at` is not a field here: the outbox stamps it with the database transaction time.
Envelopes carry IDs, enums and reasons only: never secret values, credentials, hashes of
credentials, or raw untrusted content.
"""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

SCHEMA_VERSION = 1
_NAME = re.compile(r"[a-z]+(?:\.[a-z_]+){2}")  # <context>.<entity>.<verb>
_KEY = re.compile(r"[a-z_]{1,40}")
_MAX_TEXT = 100

PayloadValue = str | int | bool | None


class ActorType(StrEnum):
    USER = "user"
    CI_TOKEN = "ci_token"
    OPERATOR_CLI = "operator_cli"
    SYSTEM = "system"


class Channel(StrEnum):
    HTTP = "http"
    CLI = "cli"


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: str | None = None

    def __post_init__(self) -> None:
        identified = self.type in (ActorType.USER, ActorType.CI_TOKEN)
        if identified != (self.id is not None):
            raise ValueError("user and ci_token actors have an ID; others have none")


@dataclass(frozen=True)
class Origin:
    """Where the change was requested: the channel and its correlation ID (the HTTP
    request ID, or one ID per CLI run)."""

    channel: Channel
    correlation_id: str


@dataclass(frozen=True)
class Subject:
    type: str
    id: str


@dataclass(frozen=True)
class Event:
    name: str
    actor: Actor
    origin: Origin
    subject: Subject
    org_id: str | None = None
    repo_id: str | None = None
    before: str | None = None
    after: str | None = None
    reason: str | None = None
    payload: Mapping[str, PayloadValue] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError("event names are <context>.<entity>.<verb>")
        for key, value in self.payload.items():
            if not _KEY.fullmatch(key):
                raise ValueError("payload keys are short snake_case names")
            if isinstance(value, str) and len(value) > _MAX_TEXT:
                raise ValueError("payload text is limited to IDs and enum values")
            if value is not None and not isinstance(value, str | int | bool):
                raise ValueError("payload values are strings, integers or booleans")
