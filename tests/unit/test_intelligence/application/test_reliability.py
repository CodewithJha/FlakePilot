"""Reliability analytics: composition of counts over fake history rows.

The SQL store is exercised by the integration suite (real tables); here the
pure composition is verified: the flaky statistic, totals, deterministic
ordering, and the uow plumb of `Reliability` including its cutoff pass-through
(the leakage guard lives upstream in `repo_history`).
"""

import asyncio
from datetime import datetime, timedelta
from typing import Self

import pytest

from flakepilot.test_intelligence.application.reliability import (
    Direction,
    EnvironmentReliability,
    FailureCluster,
    Reliability,
    TrendReport,
    WindowTotals,
    compare_windows,
    summarize,
)
from flakepilot.test_intelligence.application.store import IdentityHistory, TrendBucket
from flakepilot.test_intelligence.domain.classification import Observation
from flakepilot.test_intelligence.domain.results import Outcome, RunSource

ORG = "11111111-1111-1111-1111-111111111111"
REPO = "22222222-2222-2222-2222-222222222222"
TEST = "33333333-3333-3333-3333-333333333333"
OTHER = "44444444-4444-4444-4444-444444444444"
BASE = datetime(2026, 10, 20, 12, 0)


def observation(
    run: str,
    received: datetime,
    attempts: tuple[Outcome, ...],
    environment: str = "ci-linux",
) -> Observation:
    return Observation(
        run_id=run,
        commit_sha="a" * 40,
        environment=environment,
        received_at=received,
        source=RunSource.CI_UPLOAD,
        attempts=attempts,
    )


def history(
    test_id: str = TEST,
    classname: str = "tests.payment",
    name: str = "test_cart_totals",
    observations: tuple[Observation, ...] = (),
) -> IdentityHistory:
    return IdentityHistory(
        test_id=test_id,
        classname=classname,
        name=name,
        observations=observations,
    )


def test_a_fail_then_pass_in_one_run_is_a_flaky_run() -> None:
    assert (
        summarize(
            (
                history(
                    observations=(
                        observation("run-1", BASE, (Outcome.FAILED, Outcome.PASSED)),
                    )
                ),
            )
        )
        .histories[0]
        .flaky_runs
        == 1
    )


def test_a_errored_then_pass_in_one_run_is_a_flaky_run() -> None:
    assert (
        summarize(
            (
                history(
                    observations=(
                        observation("run-1", BASE, (Outcome.ERRORED, Outcome.PASSED)),
                    )
                ),
            )
        )
        .histories[0]
        .flaky_runs
        == 1
    )


def test_failures_without_recovery_are_not_flaky() -> None:
    row = summarize(
        (
            history(
                observations=(
                    observation("run-1", BASE, (Outcome.PASSED, Outcome.FAILED)),
                    observation("run-2", BASE + timedelta(hours=1), (Outcome.FAILED,)),
                )
            ),
        )
    ).histories[0]
    assert row.flaky_runs == 0 and row.failed_attempts == 2 and row.runs == 2


def test_totals_environments_and_recency_are_composed_from_runs() -> None:
    row = summarize(
        (
            history(
                observations=(
                    observation(
                        "run-1", BASE, (Outcome.FAILED, Outcome.PASSED), "ci-linux"
                    ),
                    observation(
                        "run-2",
                        BASE + timedelta(hours=1),
                        (Outcome.PASSED,),
                        "ci-macos",
                    ),
                    observation(
                        "run-3",
                        BASE + timedelta(hours=2),
                        (Outcome.FAILED,),
                        "ci-linux",
                    ),
                )
            ),
        )
    ).histories[0]
    assert row.runs == 3
    assert row.attempts == 4 and row.failed_attempts == 2 and row.flaky_runs == 1
    assert row.environments == ("ci-linux", "ci-macos")
    assert (
        row.first_received_at == BASE
        and row.last_received_at == row.first_received_at + timedelta(hours=2)
    )


def test_ordering_is_most_flaky_then_most_failed_then_name() -> None:
    report = summarize(
        (
            history(
                test_id="c",
                classname="tests.z",
                name="a_never_fails",
                observations=(observation("r", BASE, (Outcome.PASSED,)),),
            ),
            history(
                test_id="a",
                classname="tests.a",
                name="z_flaky",
                observations=(
                    observation("r", BASE, (Outcome.FAILED, Outcome.PASSED)),
                ),
            ),
            history(
                test_id="b",
                classname="tests.a",
                name="y_failing",
                observations=(
                    observation("r", BASE, (Outcome.FAILED, Outcome.FAILED)),
                ),
            ),
        )
    )
    assert [h.test_id for h in report.histories] == ["a", "b", "c"]


def test_an_identity_without_runs_contributes_no_row() -> None:
    report = summarize((history(observations=()),))
    assert report.histories == ()


def test_an_empty_history_is_an_empty_report() -> None:
    report = summarize(())
    assert report.histories == ()


def test_a_null_received_at_does_not_break_the_sort() -> None:
    row = summarize(
        (
            history(
                observations=(
                    observation("run-1", BASE, (Outcome.FAILED, Outcome.PASSED)),
                    observation(
                        "run-2",
                        BASE + timedelta(hours=1),
                        (Outcome.PASSED,),
                    ),
                )
            ),
        )
    ).histories[0]
    assert row.first_received_at == BASE


def test_reliability_passes_scope_and_cutoff_to_the_store() -> None:
    """The use case's only job: authorized scope and the cutoff, untouched."""
    seen: dict[str, object] = {}

    class FakeStore:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def repo_history(
            self, *, org_id: str, repo_id: str, cutoff: datetime, max_tests: int
        ) -> tuple[IdentityHistory, ...]:
            seen.update(
                {
                    "pair": (org_id, repo_id),
                    "cutoff": cutoff,
                    "max_tests": max_tests,
                }
            )
            return ()

    unit_of_work = lambda: FakeStore()
    result = asyncio.run(
        Reliability(unit_of_work, max_tests=7).report(  # type: ignore[arg-type]
            org_id=ORG, repo_id=REPO, cutoff=BASE
        )
    )
    assert seen == {"pair": (ORG, REPO), "cutoff": BASE, "max_tests": 7}
    assert result.histories == ()


def test_environment_rows_show_where_a_test_fails() -> None:
    report = summarize(
        (
            history(
                observations=(
                    observation("r1", BASE, (Outcome.FAILED, Outcome.PASSED), "macos"),
                    observation("r2", BASE, (Outcome.FAILED,), "macos"),
                    observation("r3", BASE, (Outcome.PASSED,), "linux"),
                )
            ),
        )
    )
    assert report.environments == (
        EnvironmentReliability(TEST, "linux", runs=1, failed_runs=0, flaky_runs=0),
        EnvironmentReliability(TEST, "macos", runs=2, failed_runs=2, flaky_runs=1),
    )


def test_tests_failing_in_the_same_runs_form_a_cluster() -> None:
    def failing_in(test_id: str, *runs: str) -> IdentityHistory:
        return history(
            test_id=test_id,
            name=test_id,
            observations=tuple(
                observation(run, BASE, (Outcome.FAILED,)) for run in runs
            )
            + (observation("green", BASE, (Outcome.PASSED,)),),
        )

    report = summarize(
        (
            failing_in("b", "r1", "r2"),
            failing_in("a", "r1", "r2"),
            failing_in("c", "r1", "r2", "r3"),  # a different failure set
            failing_in("d", "r1"),  # one failing run is not repetition
            failing_in("e", "r1"),
        )
    )
    assert report.clusters == (
        FailureCluster(run_ids=("r1", "r2"), test_ids=("a", "b")),
    )


def bucket(
    env: str = "ci-linux", test_runs: int = 10, failed: int = 1, flaky: int = 0
) -> TrendBucket:
    return TrendBucket(BASE, env, 1, test_runs, failed, flaky)


def test_window_comparison_is_exact_arithmetic() -> None:
    def compare(
        current: tuple[TrendBucket, ...],
        previous: tuple[TrendBucket, ...],
        truncated: bool = False,
    ) -> TrendReport:
        return compare_windows(
            bucket="day",
            start=BASE,
            end=BASE,
            current=current,
            previous=previous,
            truncated=truncated,
        )

    worse = compare(
        (bucket(test_runs=10, failed=2),), (bucket(test_runs=20, failed=2),)
    )
    assert worse.failure_direction is Direction.REGRESSED
    assert worse.flaky_direction is Direction.UNCHANGED  # 0/10 vs 0/20
    # 1/3 vs 2/6 is equal; floats would be tempted to disagree.
    same = compare((bucket(test_runs=3, failed=1),), (bucket(test_runs=6, failed=2),))
    assert same.failure_direction is Direction.UNCHANGED
    better = compare((bucket(flaky=0),), (bucket(flaky=5),))
    assert better.flaky_direction is Direction.IMPROVED
    assert compare((bucket(),), ()).failure_direction is Direction.NO_DATA
    partial = compare((bucket(),), (bucket(),), truncated=True)
    assert partial.failure_direction is Direction.INCOMPLETE
    assert partial.flaky_direction is Direction.INCOMPLETE


def test_window_totals_and_environments_sum_the_cells() -> None:
    report = compare_windows(
        bucket="week",
        start=BASE,
        end=BASE,
        current=(
            bucket("linux", 4, 1, 1),
            bucket("macos", 6, 3, 0),
            bucket("linux", 2, 0, 0),
        ),
        previous=(),
        truncated=False,
    )
    assert report.current == WindowTotals(
        runs=3, test_runs=12, failed_test_runs=4, flaky_test_runs=1
    )
    assert report.previous == WindowTotals(0, 0, 0, 0)
    assert report.environments == (
        ("linux", WindowTotals(2, 6, 1, 1)),
        ("macos", WindowTotals(1, 6, 3, 0)),
    )


class TrendStore:
    def __init__(self, cells: int) -> None:
        self.calls: list[dict[str, object]] = []
        self._cells = cells

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def failure_trend(self, **kwargs: object) -> tuple[TrendBucket, ...]:
        self.calls.append(kwargs)
        return tuple(bucket() for _ in range(self._cells))


def test_trend_reads_two_adjacent_windows_in_scope() -> None:
    store = TrendStore(cells=1)
    report = asyncio.run(
        Reliability(lambda: store, max_cells=5).trend(  # type: ignore[arg-type,return-value]
            org_id=ORG, repo_id=REPO, end=BASE, window=timedelta(days=7), bucket="week"
        )
    )
    start = BASE - timedelta(days=7)
    assert store.calls == [
        {
            "org_id": ORG,
            "repo_id": REPO,
            "start": start,
            "end": BASE,
            "bucket": "week",
            "limit": 6,
        },
        {
            "org_id": ORG,
            "repo_id": REPO,
            "start": start - timedelta(days=7),
            "end": start,
            "bucket": "week",
            "limit": 6,
        },
    ]
    assert not report.truncated
    assert report.failure_direction is Direction.UNCHANGED


def test_trend_flags_a_window_over_the_cell_bound() -> None:
    store = TrendStore(cells=3)
    report = asyncio.run(
        Reliability(lambda: store, max_cells=2).trend(  # type: ignore[arg-type,return-value]
            org_id=ORG, repo_id=REPO, end=BASE, window=timedelta(days=1)
        )
    )
    assert report.truncated and len(report.buckets) == 2
    assert report.failure_direction is Direction.INCOMPLETE


def test_trend_windows_are_bounded() -> None:
    use_case = Reliability(lambda: TrendStore(0))  # type: ignore[arg-type,return-value]
    for window in (timedelta(0), timedelta(days=367)):
        with pytest.raises(ValueError, match="trend window"):
            asyncio.run(
                use_case.trend(org_id=ORG, repo_id=REPO, end=BASE, window=window)
            )
