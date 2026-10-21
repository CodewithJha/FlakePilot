"""Values that may be handed to an execution host (ADR-004).

The field set is the whole contract. A database URL, a GitHub key, a token,
and a secret do not fit. This module does not open a connection or start a
process. No implementation in this repository has a host to send the value to.
"""

import uuid
from dataclasses import dataclass

from flakepilot.execution.domain.reproduction import selector_ok, sha_ok

# Quotas match the control-plane settings bounds. They are not host enforcement.
WALL_MIN, WALL_MAX = 5, 120
MEMORY_MIN, MEMORY_MAX = 64, 2048
PIDS_MIN, PIDS_MAX = 8, 256

PLANE_FIELDS = frozenset(
    {
        "execution_id",
        "org_id",
        "repo_id",
        "sha",
        "selector",
        "wall_seconds",
        "memory_mb",
        "pids",
        "source_sha256",
    }
)


class PlaneRejected(ValueError):
    """The handoff was rejected before it could leave the control plane."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class PlaneRequest:
    """One idempotent handoff. `execution_id` is the replay key."""

    execution_id: str
    org_id: str
    repo_id: str
    sha: str
    selector: str
    wall_seconds: int
    memory_mb: int
    pids: int
    source_sha256: str


@dataclass(frozen=True)
class PlaneReport:
    """A refusal or, later, an untrusted runtime claim. Never evidence by itself."""

    status: str
    reason: str


def plane_request(
    *,
    execution_id: str,
    org_id: str,
    repo_id: str,
    sha: str,
    selector: str,
    wall_seconds: int,
    memory_mb: int,
    pids: int,
    source_sha256: str,
) -> PlaneRequest:
    """Reject anything outside the field set and the quota bounds."""
    if not (
        _uuid(execution_id)
        and _uuid(org_id)
        and _uuid(repo_id)
        and sha_ok(sha)
        and selector_ok(selector)
        and isinstance(wall_seconds, int)
        and isinstance(memory_mb, int)
        and isinstance(pids, int)
        and WALL_MIN <= wall_seconds <= WALL_MAX
        and MEMORY_MIN <= memory_mb <= MEMORY_MAX
        and PIDS_MIN <= pids <= PIDS_MAX
        and len(source_sha256) == 64
        and source_sha256.lower() == source_sha256
        and all(character in "0123456789abcdef" for character in source_sha256)
    ):
        raise PlaneRejected("invalid_request")
    return PlaneRequest(
        execution_id=execution_id,
        org_id=org_id,
        repo_id=repo_id,
        sha=sha,
        selector=selector,
        wall_seconds=wall_seconds,
        memory_mb=memory_mb,
        pids=pids,
        source_sha256=source_sha256,
    )


def unavailable_report() -> PlaneReport:
    """No execution host accepted the handoff. Nothing ran."""
    return PlaneReport("unavailable", "execution_host_unavailable")
