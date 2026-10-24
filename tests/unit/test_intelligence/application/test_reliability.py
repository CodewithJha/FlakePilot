"""Reliability analytics: composition of counts over fake history rows.

The SQL store is exercised by the integration suite (real tables); here the
pure composition is verified: the flaky statistic, totals, deterministic
ordering, and the uow plumb of `Reliability` including its cutoff pass-through
(the leakage guard lives upstream in `repo_history`).
"""

import asyncio
from datetime import datetime, timedelta
from typing import Self

from flakepilot.test_intelligence.application.reliability import (
    Reliability,
    summarize,
)
from flakepilot.test_intelligence.application.store import IdentityHistory
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
