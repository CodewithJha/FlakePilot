"""Diagnosis persistence port. The SQL adapter never commits."""

from dataclasses import dataclass
from typing import Protocol

from flakepilot.diagnosis.domain.output import DataPolicy, DiagnosisOutcome
from flakepilot.shared_kernel.events import Event

__all__ = [  # DataPolicy is re-exported: it is the level an AiPolicy is set to
    "IN_FLIGHT",
    "AiPolicy",
    "AiRequestRecord",
    "DataPolicy",
    "DiagStore",
]

# The `validation_reason` of a request recorded before its provider call. A row
# left with it means the call was attempted and no answer was ever recorded.
IN_FLIGHT = "awaiting_provider"


@dataclass(frozen=True)
class AiPolicy:
    """Org AI policy. A missing row means disabled and no raw retention."""

    level: DataPolicy
    retain_raw: bool


@dataclass(frozen=True)
class AiRequestRecord:
    """Metadata for one assist attempt. Hypothesis text is not a column."""

    id: str
    org_id: str
    repo_id: str
    actor_id: str
    policy_level: DataPolicy
    retain_raw: bool
    provider: str
    model: str | None
    payload_sha256: bytes
    payload_size: int
    outcome: DiagnosisOutcome
    validation_reason: str | None
    linked_count: int
    unverified_count: int
    contradicted_count: int
    raw_output: str | None


class DiagStore(Protocol):
    async def policy(self, org_id: str) -> AiPolicy: ...

    async def set_policy(self, org_id: str, policy: AiPolicy) -> None: ...

    async def record_request(self, request: AiRequestRecord) -> None: ...

    async def finish_request(self, request: AiRequestRecord) -> None:
        """Overwrite the in-flight row with the same id and org; raises when
        there is none, so a finished row is never rewritten."""
        ...

    async def append_event(self, event: Event) -> None: ...
