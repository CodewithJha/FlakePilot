"""Failure classification (PRODUCT-SCOPE §5.1, D4, D5): deterministic, versioned rules over
execution evidence. No AI, no confidence scores. Same observations, same result.

Rule set `ti-classification/1` (thresholds provisional, ADR-013), for one test identity:
1. Evidence is CI-uploaded runs only (D5); skipped attempts are ignored.
2. Target revision: the one asked for, else the most recently first-seen SHA with a
   failing (failed or errored) attempt. None: `inconclusive` / `insufficient_history`.
3. `flaky`: at the target SHA, in one environment, at least one passing and one failing
   attempt (across retries or runs).
4. Otherwise passes at the target SHA in another environment: `inconclusive` /
   `conflicting_signals`.
5. Every attempt at the target SHA fails, at least `MIN_FAILING_ATTEMPTS` of them,
   all the same kind (failed or errored: the v1 stand-in for one failure signature);
   fewer: `insufficient_history`; mixed kinds: `conflicting_signals`.
6. A passing baseline is a pass at another SHA first seen earlier, in an environment
   that fails at the target. None: `inconclusive` / `no_passing_baseline` (D4).
7. Flaky evidence at any other SHA in those environments: `conflicting_signals`.
   Otherwise `deterministic_regression`.
`infrastructure_failure` needs explicit environment signals, which CI uploads do not carry
in v1, so these rules never produce it (ADR-013). Revision order is first-seen ingestion
order until code-host ancestry is available (ADR-013).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from flakepilot.test_intelligence.domain.results import Outcome, RunSource

RULE_VERSION = "ti-classification/1"
MIN_FAILING_ATTEMPTS = 2

EvidenceValue = str | int | list[str] | None


class FailureClass(StrEnum):
    FLAKY = "flaky"
    DETERMINISTIC_REGRESSION = "deterministic_regression"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    INCONCLUSIVE = "inconclusive"


class Reason(StrEnum):
    INSUFFICIENT_HISTORY = "insufficient_history"
    NO_PASSING_BASELINE = "no_passing_baseline"
    CONFLICTING_SIGNALS = "conflicting_signals"


@dataclass(frozen=True)
class Observation:
    """One run's attempts for the test identity."""

    run_id: str
    commit_sha: str
    environment: str
    received_at: datetime
    source: RunSource
    attempts: tuple[Outcome, ...]


@dataclass(frozen=True)
class Classification:
    failure_class: FailureClass
    reason: Reason | None
    target_sha: str | None
    evidence: Mapping[str, EvidenceValue] = field(default_factory=dict)
    rule_version: str = RULE_VERSION


def classify(
    observations: Iterable[Observation], target_sha: str | None = None
) -> Classification:
    all_runs = list(observations)
    runs = [o for o in all_runs if o.source is RunSource.CI_UPLOAD]
    excluded = sorted(o.run_id for o in all_runs if o.source is not RunSource.CI_UPLOAD)
    first_seen: dict[str, datetime] = {}
    for run in runs:
        seen = first_seen.get(run.commit_sha)
        first_seen[run.commit_sha] = (
            min(seen, run.received_at) if seen else run.received_at
        )

    def outcome(
        cls: FailureClass, reason: Reason | None, **facts: EvidenceValue
    ) -> Classification:
        return Classification(
            cls, reason, target_sha, {**facts, "excluded_runs": excluded}
        )

    if target_sha is None:
        failing = [r for r in runs if any(a.failing for a in r.attempts)]
        if not failing:
            return outcome(FailureClass.INCONCLUSIVE, Reason.INSUFFICIENT_HISTORY)
        target_sha = max(
            failing, key=lambda r: (first_seen[r.commit_sha], r.commit_sha)
        ).commit_sha

    at_target = [r for r in runs if r.commit_sha == target_sha]
    by_env = _attempts_by_environment(at_target)
    if not by_env:
        return outcome(FailureClass.INCONCLUSIVE, Reason.INSUFFICIENT_HISTORY)
    target_runs = sorted(r.run_id for r in at_target)
    counts = _counts(a for attempts in by_env.values() for a in attempts)

    for environment, attempts in sorted(by_env.items()):
        if Outcome.PASSED in attempts and any(a.failing for a in attempts):
            return outcome(
                FailureClass.FLAKY,
                None,
                environment=environment,
                target_runs=target_runs,
                **counts,
            )
    failing_envs = sorted(e for e, a in by_env.items() if any(x.failing for x in a))
    if not failing_envs:
        return outcome(FailureClass.INCONCLUSIVE, Reason.INSUFFICIENT_HISTORY, **counts)
    if counts["passed"]:
        return outcome(
            FailureClass.INCONCLUSIVE,
            Reason.CONFLICTING_SIGNALS,
            target_runs=target_runs,
            **counts,
        )
    if counts["failed"] + counts["errored"] < MIN_FAILING_ATTEMPTS:
        return outcome(
            FailureClass.INCONCLUSIVE,
            Reason.INSUFFICIENT_HISTORY,
            target_runs=target_runs,
            **counts,
        )
    if counts["failed"] and counts["errored"]:
        return outcome(
            FailureClass.INCONCLUSIVE,
            Reason.CONFLICTING_SIGNALS,
            target_runs=target_runs,
            **counts,
        )

    earlier = [
        r
        for r in runs
        if r.environment in failing_envs
        and r.commit_sha != target_sha
        and first_seen[r.commit_sha] < first_seen[target_sha]
    ]
    baseline = [r for r in earlier if Outcome.PASSED in r.attempts]
    if not baseline:
        return outcome(
            FailureClass.INCONCLUSIVE,
            Reason.NO_PASSING_BASELINE,
            target_runs=target_runs,
            **counts,
        )
    other = [
        r for r in runs if r.environment in failing_envs and r.commit_sha != target_sha
    ]
    for (sha, _), attempts in _attempts_by_sha_and_environment(other).items():
        if Outcome.PASSED in attempts and any(a.failing for a in attempts):
            return outcome(
                FailureClass.INCONCLUSIVE,
                Reason.CONFLICTING_SIGNALS,
                target_runs=target_runs,
                flaky_sha=sha,
                **counts,
            )
    latest_pass = max(baseline, key=lambda r: (first_seen[r.commit_sha], r.run_id))
    return outcome(
        FailureClass.DETERMINISTIC_REGRESSION,
        None,
        target_runs=target_runs,
        baseline_sha=latest_pass.commit_sha,
        baseline_runs=sorted(
            r.run_id for r in baseline if r.commit_sha == latest_pass.commit_sha
        ),
        **counts,
    )


def _attempts_by_environment(runs: Iterable[Observation]) -> dict[str, list[Outcome]]:
    grouped: dict[str, list[Outcome]] = {}
    for run in runs:
        played = [a for a in run.attempts if a is not Outcome.SKIPPED]
        if played:
            grouped.setdefault(run.environment, []).extend(played)
    return grouped


def _attempts_by_sha_and_environment(
    runs: Iterable[Observation],
) -> dict[tuple[str, str], list[Outcome]]:
    grouped: dict[tuple[str, str], list[Outcome]] = {}
    for run in runs:
        grouped.setdefault((run.commit_sha, run.environment), []).extend(run.attempts)
    return grouped


def _counts(attempts: Iterable[Outcome]) -> dict[str, int]:
    counts = {o.value: 0 for o in (Outcome.PASSED, Outcome.FAILED, Outcome.ERRORED)}
    for attempt in attempts:
        if attempt is not Outcome.SKIPPED:
            counts[attempt.value] += 1
    return counts
