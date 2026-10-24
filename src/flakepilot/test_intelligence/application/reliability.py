"""Historical reliability analytics over stored attempt history (ADR-017 §5).

Reads only what is already stored — `ti.test_identities` and `ti.runs` /
`ti.test_results` — and computes deterministic, explainable statistics: each
test's attempt history across runs and environments, its failure counts, and
its flaky-run count. Scope is strictly (org, repo): another org's or repo's
rows are never read (the store answers unknown pairs with an empty history).

Everything here is a pure composition of stored rows — trends are counts,
never scores, and the rule set `ti-classification/1` stays the *authoritative*
verdict mechanism. A "flaky run" here is the mechanical statistic the rule set
classifies on: at least one failing or errored attempt followed by a passing
attempt in the same run. This module reports the statistic; it never issues
verdicts.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime

from flakepilot.test_intelligence.application.prioritization import (
    Prioritization,
    prioritize,
)
from flakepilot.test_intelligence.application.store import IdentityHistory, TiStore
from flakepilot.test_intelligence.domain.results import Outcome

UnitOfWork = Callable[[], AbstractAsyncContextManager[TiStore]]


@dataclass(frozen=True)
class TestReliability:
    """One identity's stored-history statistics (cutoffs apply to the input)."""

    test_id: str
    classname: str
    name: str
    environments: tuple[str, ...]
    runs: int
    attempts: int
    failed_attempts: int
    flaky_runs: int
    first_received_at: datetime | None
    last_received_at: datetime | None

    def __post_init__(self) -> None:
        if self.runs < 1:
            raise ValueError("a reliability row has at least one run")
        if self.failed_attempts > self.attempts:
            raise ValueError("failed attempts cannot exceed attempts")


@dataclass(frozen=True)
class ReliabilityReport:
    """The pair's history report, most interesting first (flaky runs, then
    failures, then name) — the maintainers' read order."""

    histories: tuple[TestReliability, ...]

    def __post_init__(self) -> None:
        if len(self.histories) > 1000:
            raise ValueError("the report is bounded by the store's identity page")


def _flaky_run(attempts: tuple[Outcome, ...]) -> bool:
    """The rule set's mechanical flaky evidence: a failure/errored attempt later
    recovered inside one run. Statistics only; verdicts belong to the rule set."""
    seen_failure = False
    for attempt in attempts:
        if attempt in (Outcome.FAILED, Outcome.ERRORED):
            seen_failure = True
        elif seen_failure and attempt == Outcome.PASSED:
            return True
    return False


def summarize(histories: tuple[IdentityHistory, ...]) -> ReliabilityReport:
    """Pure: no store access, no clock, deterministic order."""
    rows: list[TestReliability] = []
    for history in histories:
        # received_at is nullable at the store level; guard the sort against it.
        received = sorted(o.received_at for o in history.observations if o.received_at)
        runs = len(history.observations)
        if runs == 0:
            continue  # an identity with no stored runs carries no statistics
        attempts = sum(len(o.attempts) for o in history.observations)
        failures = sum(
            1
            for o in history.observations
            for attempt in o.attempts
            if attempt in (Outcome.FAILED, Outcome.ERRORED)
        )
        flaky = sum(1 for o in history.observations if _flaky_run(o.attempts))
        rows.append(
            TestReliability(
                test_id=history.test_id,
                classname=history.classname,
                name=history.name,
                environments=tuple(
                    sorted({o.environment for o in history.observations})
                ),
                runs=runs,
                attempts=attempts,
                failed_attempts=failures,
                flaky_runs=flaky,
                first_received_at=received[0] if received else None,
                last_received_at=received[-1] if received else None,
            )
        )
    rows.sort(key=lambda r: (-r.flaky_runs, -r.failed_attempts, r.classname, r.name))
    return ReliabilityReport(histories=tuple(rows))


class Reliability:
    """The analytics use case: authorized read in, deterministic report out."""

    def __init__(self, unit_of_work: UnitOfWork, max_tests: int = 100) -> None:
        self._unit_of_work = unit_of_work
        self._max_tests = max_tests

    async def report(
        self, *, org_id: str, repo_id: str, cutoff: datetime
    ) -> ReliabilityReport:
        async with self._unit_of_work() as store:
            histories = await store.repo_history(
                org_id=org_id,
                repo_id=repo_id,
                cutoff=cutoff,
                max_tests=self._max_tests,
            )
        return summarize(histories)

    async def prioritized(
        self, *, org_id: str, repo_id: str, cutoff: datetime
    ) -> Prioritization:
        """The report composed with the deterministic prioritization baseline —
        one cutoff, one read, ranks from counts only."""
        async with self._unit_of_work() as store:
            histories = await store.repo_history(
                org_id=org_id,
                repo_id=repo_id,
                cutoff=cutoff,
                max_tests=self._max_tests,
            )
        return prioritize(histories)
