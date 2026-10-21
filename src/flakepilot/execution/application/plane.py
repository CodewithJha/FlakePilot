"""Execution-plane port. The production adapter refuses every handoff.

A later host would implement `ExecutionPlane` on a machine that does not hold
control-plane secrets. This process is not that machine. `submit` does not
open a socket, mint a token, or start a runtime. `reclaim` does not delete
runtime resources and does not report that it did.
"""

from typing import Protocol

from flakepilot.execution.domain.plane import (
    PlaneReport,
    PlaneRequest,
    unavailable_report,
)

__all__ = [
    "ExecutionPlane",
    "ReapResult",
    "RuntimeReaper",
    "UnavailableExecutionPlane",
    "UnavailableRuntimeReaper",
]


class ExecutionPlane(Protocol):
    """Accept one validated request and return an untrusted report."""

    async def submit(self, request: PlaneRequest) -> PlaneReport: ...


class UnavailableExecutionPlane:
    """Fail closed. Repeated submits do not run and do not change state."""

    async def submit(self, request: PlaneRequest) -> PlaneReport:
        if not isinstance(request, PlaneRequest):
            raise TypeError("execution plane input is a PlaneRequest")
        return unavailable_report()


class ReapResult:
    """`reclaimed` is a count of runtime resources removed. Zero means none."""

    __slots__ = ("reason", "reclaimed")

    def __init__(self, reclaimed: int, reason: str) -> None:
        self.reclaimed = reclaimed
        self.reason = reason


class RuntimeReaper(Protocol):
    """Recover abandoned runtime resources. Must not touch an active run."""

    def reclaim(self) -> ReapResult: ...


class UnavailableRuntimeReaper:
    """No runtime is reachable from this process. Reclaim is a visible refusal."""

    def reclaim(self) -> ReapResult:
        return ReapResult(0, "runtime_reaper_unavailable")
