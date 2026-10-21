from datetime import UTC, datetime, timedelta

from flakepilot.test_intelligence.domain.classification import (
    RULE_VERSION,
    FailureClass,
    Observation,
    Reason,
    classify,
)
from flakepilot.test_intelligence.domain.results import Outcome, RunSource

P, F, E, S = Outcome.PASSED, Outcome.FAILED, Outcome.ERRORED, Outcome.SKIPPED
T0 = datetime(2026, 10, 1, tzinfo=UTC)
OLD, NEW = "1" * 40, "2" * 40


def run(
    n: int,
    sha: str,
    *attempts: Outcome,
    env: str = "linux",
    source: RunSource = RunSource.CI_UPLOAD,
) -> Observation:
    return Observation(f"run-{n}", sha, env, T0 + timedelta(hours=n), source, attempts)


def verdict(
    *runs: Observation, target: str | None = None
) -> tuple[FailureClass, Reason | None]:
    result = classify(runs, target_sha=target)
    assert result.rule_version == RULE_VERSION == "ti-classification/1"
    return result.failure_class, result.reason


def test_pass_and_fail_at_one_sha_in_one_environment_is_flaky() -> None:
    assert verdict(run(1, NEW, F, P)) == (FailureClass.FLAKY, None)  # retry in one run
    assert verdict(run(1, NEW, F), run(2, NEW, P)) == (FailureClass.FLAKY, None)


def test_consistent_failure_after_an_earlier_pass_is_a_deterministic_regression() -> (
    None
):
    result = classify([run(1, OLD, P), run(2, NEW, F), run(3, NEW, F)])
    assert (result.failure_class, result.reason) == (
        FailureClass.DETERMINISTIC_REGRESSION,
        None,
    )
    assert result.target_sha == NEW
    assert result.evidence["baseline_sha"] == OLD
    assert result.evidence["baseline_runs"] == ["run-1"]
    assert result.evidence["target_runs"] == ["run-2", "run-3"]
    assert result.evidence["failed"] == 2


def test_consistent_failure_without_a_passing_baseline_is_inconclusive_d4() -> None:
    assert verdict(run(1, NEW, F), run(2, NEW, F, F)) == (
        FailureClass.INCONCLUSIVE,
        Reason.NO_PASSING_BASELINE,
    )
    # A pass only at a later revision is not a baseline.
    assert verdict(run(1, NEW, F, F), run(2, OLD, P), target=NEW) == (
        FailureClass.INCONCLUSIVE,
        Reason.NO_PASSING_BASELINE,
    )


def test_sandbox_runs_are_never_evidence_d5() -> None:
    # A sandbox pass would make it look flaky; a sandbox pass at OLD would be a baseline.
    sandbox_pass = run(3, NEW, P, source=RunSource.SANDBOX)
    sandbox_baseline = run(0, OLD, P, source=RunSource.SANDBOX)
    result = classify([run(1, NEW, F), run(2, NEW, F), sandbox_pass, sandbox_baseline])
    assert (result.failure_class, result.reason) == (
        FailureClass.INCONCLUSIVE,
        Reason.NO_PASSING_BASELINE,
    )
    assert result.evidence["excluded_runs"] == ["run-0", "run-3"]


def test_too_little_evidence_is_insufficient_history() -> None:
    assert verdict() == (FailureClass.INCONCLUSIVE, Reason.INSUFFICIENT_HISTORY)
    assert verdict(run(1, NEW, P)) == (
        FailureClass.INCONCLUSIVE,
        Reason.INSUFFICIENT_HISTORY,
    )
    assert verdict(run(1, OLD, P), run(2, NEW, F)) == (  # one failing attempt
        FailureClass.INCONCLUSIVE,
        Reason.INSUFFICIENT_HISTORY,
    )
    assert verdict(run(1, NEW, S), run(2, NEW, S), target=NEW) == (
        FailureClass.INCONCLUSIVE,
        Reason.INSUFFICIENT_HISTORY,
    )
    assert verdict(run(1, NEW, F, F), target="3" * 40) == (
        FailureClass.INCONCLUSIVE,
        Reason.INSUFFICIENT_HISTORY,
    )


def test_conflicting_signals_are_inconclusive_never_a_guess() -> None:
    conflicting = (FailureClass.INCONCLUSIVE, Reason.CONFLICTING_SIGNALS)
    # Passes in another environment at the same SHA.
    assert verdict(run(1, NEW, F, F), run(2, NEW, P, env="mac")) == conflicting
    # Failed and errored mixed: no single failure kind.
    assert verdict(run(1, OLD, P), run(2, NEW, F), run(3, NEW, E)) == conflicting
    # Flaky at the baseline revision.
    assert verdict(run(1, OLD, P, F), run(2, NEW, F), run(3, NEW, F)) == conflicting


def test_errors_alone_are_never_infrastructure_failures_in_v1() -> None:
    result = classify([run(1, OLD, P), run(2, NEW, E), run(3, NEW, E)])
    assert result.failure_class is FailureClass.DETERMINISTIC_REGRESSION
    for runs in (
        [run(1, NEW, E), run(2, NEW, E)],
        [run(1, NEW, E, P)],
        [run(1, NEW, E)],
    ):
        assert classify(runs).failure_class is not FailureClass.INFRASTRUCTURE_FAILURE


def test_the_target_defaults_to_the_latest_failing_revision_and_is_deterministic() -> (
    None
):
    history = [run(1, OLD, F, P), run(2, NEW, F), run(3, NEW, F), run(4, "3" * 40, P)]
    first = classify(history)
    assert first.target_sha == NEW
    assert classify(list(reversed(history))) == first
    assert classify(history, target_sha=OLD).failure_class is FailureClass.FLAKY
