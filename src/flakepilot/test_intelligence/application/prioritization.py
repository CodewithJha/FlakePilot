"""Deterministic failure prioritization over stored history (ADR-017 §5; goal 12).

The transparent baseline any probabilistic model (TabPFN or similar) must be
evaluated against before it could ever be selected. No model is introduced
here: the data — stored attempt history — already answers the maintainers'
practical question ("what should I look at first?") with explainable
arithmetic.

Design rules, per the goal:
- **Transparent features only.** Every input is a count of stored outcomes:
  flaky runs, failed attempts, and recency of the last failure. Nothing else.
- **No leakage.** The caller passes histories already cut at a cutoff; nothing
  here reads or infers anything after it.
- **No invented confidence.** The output is a rank with its reason — counts
  and a coverage note — never a probability, weight, or precision claim. A
  probabilistic successor must record its evaluation on real stored history
  before it may claim any.
- **Honest emptiness.** No stored history produces no ranking and a note —
  never a fabricated ordering.

Tie order is total and deterministic. Classification authority stays with
`ti-classification/1`; this module only orders tests for attention.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from flakepilot.test_intelligence.application.reliability import (
    ReliabilityReport,
    summarize,
)
from flakepilot.test_intelligence.application.store import IdentityHistory
from flakepilot.test_intelligence.domain.results import Outcome

# The mechanical flaky evidence, matched to reliability.py and the rule set.
_WEIGHTS = {"flaky_run": 4, "failed_attempt": 2}


@dataclass(frozen=True)
class Priority:
    """One test's rank and the counts behind it — no probability, no score."""

    rank: int
    test_id: str
    classname: str
    name: str
    flaky_runs: int
    failed_attempts: int
    attempts: int
    last_failure_at: datetime | None
    last_run_at: datetime | None


@dataclass(frozen=True)
class Prioritization:
    """The ranked result: tests worth attention first, then the honest note."""

    ranked: tuple[Priority, ...]
    note: str


def _when(value: datetime | None) -> float:
    """A homogeneous, deterministic sort key: instant, `None` last."""
    if value is None:
        return -math.inf
    return value.timestamp()


def _last_failure(history: IdentityHistory) -> datetime | None:
    """The most recent cutoff-bounded run in which any attempt failed."""
    return max(
        (
            o.received_at
            for o in history.observations
            if o.received_at
            and any(
                attempt in (Outcome.FAILED, Outcome.ERRORED) for attempt in o.attempts
            )
        ),
        default=None,
    )


def prioritize(histories: tuple[IdentityHistory, ...]) -> Prioritization:
    """Pure: deterministic order over the given (already cutoff-bounded) rows."""
    if not histories:
        return Prioritization(
            ranked=(),
            note=(
                "No stored history at or before the cutoff: nothing to "
                "prioritize and no model output may be invented from silence."
            ),
        )
    report: ReliabilityReport = summarize(histories)
    by_id = {h.test_id: h for h in histories}
    entries: list[Priority] = []
    for row in report.histories:
        history = by_id[row.test_id]
        entries.append(
            Priority(
                rank=0,
                test_id=row.test_id,
                classname=row.classname,
                name=row.name,
                flaky_runs=row.flaky_runs,
                failed_attempts=row.failed_attempts,
                attempts=row.attempts,
                last_failure_at=_last_failure(history),
                last_run_at=max(
                    (o.received_at for o in history.observations if o.received_at),
                    default=None,
                ),
            )
        )
    entries.sort(
        key=lambda p: (
            -_WEIGHTS["flaky_run"] * p.flaky_runs
            - _WEIGHTS["failed_attempt"] * p.failed_attempts,
            -_when(p.last_failure_at),
            -_when(p.last_run_at),
            p.classname,
            p.name,
            p.test_id,
        )
    )
    ranked = tuple(
        Priority(
            rank=index + 1,
            test_id=entry.test_id,
            classname=entry.classname,
            name=entry.name,
            flaky_runs=entry.flaky_runs,
            failed_attempts=entry.failed_attempts,
            attempts=entry.attempts,
            last_failure_at=entry.last_failure_at,
            last_run_at=entry.last_run_at,
        )
        for index, entry in enumerate(entries)
    )
    return Prioritization(
        ranked=ranked,
        note=(
            f"{len(ranked)} of {len(histories)} stored identities ranked by "
            f"recorded counts only ({_WEIGHTS['flaky_run']} per flaky run, "
            f"{_WEIGHTS['failed_attempt']} per failed attempt, most recent "
            "failure first in ties). Rank, not probability."
        ),
    )
