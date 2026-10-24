"""Historical reliability analytics over stored attempt history (ADR-017 §5).

Reads only what is already stored — `ti.test_identities` and `ti.runs` /
`ti.test_results` — and computes deterministic, explainable statistics: each
test's attempt history across runs and environments, its failure counts, and
its flaky-run count; per-environment counts; clusters of tests failing in the
same runs; and day/week failure and flaky trends against the previous window
(plain PostgreSQL `date_trunc`, so vanilla PostgreSQL and Tiger Cloud alike). Scope is strictly (org, repo): another org's or repo's
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
from datetime import datetime, timedelta
from enum import StrEnum

from flakepilot.test_intelligence.application.prioritization import (
    Prioritization,
    prioritize,
)
from flakepilot.test_intelligence.application.store import (
    IdentityHistory,
    TiStore,
    TrendBucket,
)
from flakepilot.test_intelligence.domain.results import Outcome

UnitOfWork = Callable[[], AbstractAsyncContextManager[TiStore]]
MAX_WINDOW = timedelta(days=366)


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
class EnvironmentReliability:
    """One identity in one environment: where a test fails, not just whether."""

    test_id: str
    environment: str
    runs: int
    failed_runs: int
    flaky_runs: int


@dataclass(frozen=True)
class FailureCluster:
    """Identities whose failing runs are exactly the same set of two or more
    runs — a shared-cause candidate (fixture, service, environment). A
    grouping of stored facts, never a diagnosis."""

    run_ids: tuple[str, ...]
    test_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReliabilityReport:
    """The pair's history report, most interesting first (flaky runs, then
    failures, then name) — the maintainers' read order."""

    histories: tuple[TestReliability, ...]
    environments: tuple[EnvironmentReliability, ...] = ()
    clusters: tuple[FailureCluster, ...] = ()

    def __post_init__(self) -> None:
        if len(self.histories) > 1000:
            raise ValueError("the report is bounded by the store's identity page")


@dataclass(frozen=True)
class WindowTotals:
    """Counts over one window; rates are left to the reader's arithmetic."""

    runs: int
    test_runs: int
    failed_test_runs: int
    flaky_test_runs: int


class Direction(StrEnum):
    """Arithmetic comparison of two rates — not a significance claim."""

    REGRESSED = "regressed"
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    NO_DATA = "no_data"  # a window without test runs has no rate
    INCOMPLETE = "incomplete"  # a window hit the cell bound; totals are partial


@dataclass(frozen=True)
class TrendReport:
    """The current window's buckets, per-environment totals, and the change in
    failure and flaky rates against the immediately preceding window."""

    bucket: str
    start: datetime
    end: datetime
    buckets: tuple[TrendBucket, ...]
    environments: tuple[tuple[str, WindowTotals], ...]
    current: WindowTotals
    previous: WindowTotals
    failure_direction: Direction
    flaky_direction: Direction
    truncated: bool


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
    return ReliabilityReport(
        histories=tuple(rows),
        environments=_environments(histories),
        clusters=_clusters(histories),
    )


def _failed(attempts: tuple[Outcome, ...]) -> bool:
    return any(attempt.failing for attempt in attempts)


def _environments(
    histories: tuple[IdentityHistory, ...],
) -> tuple[EnvironmentReliability, ...]:
    """Per (identity, environment) counts, ordered by identity then environment."""
    cells: dict[tuple[str, str], list[int]] = {}
    for history in histories:
        for o in history.observations:
            cell = cells.setdefault((history.test_id, o.environment), [0, 0, 0])
            cell[0] += 1
            cell[1] += _failed(o.attempts)
            cell[2] += _flaky_run(o.attempts)
    return tuple(
        EnvironmentReliability(test_id, environment, *counts)
        for (test_id, environment), counts in sorted(cells.items())
    )


def _clusters(histories: tuple[IdentityHistory, ...]) -> tuple[FailureCluster, ...]:
    """Identities grouped by their exact set of failing runs (two or more runs,
    two or more identities), largest shared failure set first."""
    by_runs: dict[frozenset[str], list[str]] = {}
    for history in histories:
        failing = frozenset(
            o.run_id for o in history.observations if _failed(o.attempts)
        )
        if len(failing) >= 2:
            by_runs.setdefault(failing, []).append(history.test_id)
    clusters = [
        FailureCluster(run_ids=tuple(sorted(runs)), test_ids=tuple(sorted(tests)))
        for runs, tests in by_runs.items()
        if len(tests) >= 2
    ]
    clusters.sort(key=lambda c: (-len(c.run_ids), -len(c.test_ids), c.test_ids))
    return tuple(clusters)


def _totals(buckets: tuple[TrendBucket, ...]) -> WindowTotals:
    return WindowTotals(
        runs=sum(b.runs for b in buckets),
        test_runs=sum(b.test_runs for b in buckets),
        failed_test_runs=sum(b.failed_test_runs for b in buckets),
        flaky_test_runs=sum(b.flaky_test_runs for b in buckets),
    )


def _direction(current: tuple[int, int], previous: tuple[int, int]) -> Direction:
    """Compare current/previous rates exactly, by cross-multiplication."""
    (count, total), (before, before_total) = current, previous
    if total == 0 or before_total == 0:
        return Direction.NO_DATA
    now, then = count * before_total, before * total
    if now > then:
        return Direction.REGRESSED
    if now < then:
        return Direction.IMPROVED
    return Direction.UNCHANGED


def compare_windows(
    *,
    bucket: str,
    start: datetime,
    end: datetime,
    current: tuple[TrendBucket, ...],
    previous: tuple[TrendBucket, ...],
    truncated: bool,
) -> TrendReport:
    """Pure: the two windows' cells in, the comparison out."""
    now, before = _totals(current), _totals(previous)
    environments = sorted({b.environment for b in current})
    return TrendReport(
        bucket=bucket,
        start=start,
        end=end,
        buckets=current,
        environments=tuple(
            (env, _totals(tuple(b for b in current if b.environment == env)))
            for env in environments
        ),
        current=now,
        previous=before,
        failure_direction=Direction.INCOMPLETE
        if truncated
        else _direction(
            (now.failed_test_runs, now.test_runs),
            (before.failed_test_runs, before.test_runs),
        ),
        flaky_direction=Direction.INCOMPLETE
        if truncated
        else _direction(
            (now.flaky_test_runs, now.test_runs),
            (before.flaky_test_runs, before.test_runs),
        ),
        truncated=truncated,
    )


class Reliability:
    """The analytics use case: authorized read in, deterministic report out."""

    def __init__(
        self, unit_of_work: UnitOfWork, max_tests: int = 100, max_cells: int = 1000
    ) -> None:
        self._unit_of_work = unit_of_work
        self._max_tests = max_tests
        self._max_cells = max_cells

    async def trend(
        self,
        *,
        org_id: str,
        repo_id: str,
        end: datetime,
        window: timedelta,
        bucket: str = "day",
    ) -> TrendReport:
        """`(end - window, end]` against the window before it, one read each.
        A window is bounded to `MAX_WINDOW`; each read to `max_cells` cells."""
        if not timedelta(0) < window <= MAX_WINDOW:
            raise ValueError("the trend window is longer than zero and at most a year")
        start = end - window
        async with self._unit_of_work() as store:
            current = await store.failure_trend(
                org_id=org_id,
                repo_id=repo_id,
                start=start,
                end=end,
                bucket=bucket,
                limit=self._max_cells + 1,
            )
            previous = await store.failure_trend(
                org_id=org_id,
                repo_id=repo_id,
                start=start - window,
                end=start,
                bucket=bucket,
                limit=self._max_cells + 1,
            )
        truncated = max(len(current), len(previous)) > self._max_cells
        return compare_windows(
            bucket=bucket,
            start=start,
            end=end,
            current=current[: self._max_cells],
            previous=previous[: self._max_cells],
            truncated=truncated,
        )

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
