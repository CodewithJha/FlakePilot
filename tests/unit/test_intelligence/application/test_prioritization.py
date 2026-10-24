"""Deterministic prioritization: transparent rank from stored counts.

Verifies the baseline contract: counts ranked, ties broken deterministically,
recency honoured, honest emptiness, and no probability-shaped field anywhere
in the output.
"""

from datetime import datetime, timedelta

from test_reliability import history, observation

from flakepilot.test_intelligence.application.prioritization import (
    prioritize,
)
from flakepilot.test_intelligence.domain.results import Outcome

BASE = datetime(2026, 10, 20, 12, 0)
LATER = BASE + timedelta(hours=3)


def test_no_history_means_no_ranking_and_honest_note() -> None:
    result = prioritize(())
    assert result.ranked == ()
    assert "nothing to prioritize" in result.note


def test_flaky_ranks_above_only_failing_then_never_failing() -> None:
    result = prioritize(
        (
            history(
                test_id="c",
                classname="tests.z",
                name="never_fails",
                observations=(observation("r", BASE, (Outcome.PASSED,)),),
            ),
            history(
                test_id="b",
                classname="tests.a",
                name="failing",
                observations=(observation("r", BASE, (Outcome.FAILED,)),),
            ),
            history(
                test_id="a",
                classname="tests.m",
                name="flaky",
                observations=(
                    observation("r", BASE, (Outcome.FAILED, Outcome.PASSED)),
                ),
            ),
        )
    )
    assert [p.test_id for p in result.ranked] == ["a", "b", "c"]
    assert [p.rank for p in result.ranked] == [1, 2, 3]
    assert "Rank, not probability" in result.note


def test_a_more_recent_failure_wins_equal_counts() -> None:
    result = prioritize(
        (
            history(
                test_id="old",
                classname="tests.a",
                name="old_failure",
                observations=(
                    observation("r1", BASE, (Outcome.FAILED,)),
                    observation("r2", BASE + timedelta(hours=1), (Outcome.PASSED,)),
                ),
            ),
            history(
                test_id="new",
                classname="tests.b",
                name="new_failure",
                observations=(observation("r", LATER, (Outcome.ERRORED,)),),
            ),
        )
    )
    assert result.ranked[0].test_id == "new"


def test_never_failing_sorts_behind_failing_at_zero_counts() -> None:
    """`None` failure times are ordered after any instant, never crash."""
    result = prioritize(
        (
            history(
                test_id="green",
                observations=(observation("r", LATER, (Outcome.PASSED,)),),
            ),
            history(
                test_id="red",
                observations=(observation("r", BASE, (Outcome.FAILED,)),),
            ),
        )
    )
    assert result.ranked[0].test_id == "red"


def test_the_tie_order_is_total_and_repeatable() -> None:
    pair = (
        history(
            test_id="one",
            classname="tests.a",
            name="x",
            observations=(observation("r", BASE, (Outcome.FAILED,)),),
        ),
        history(
            test_id="two",
            classname="tests.a",
            name="y",
            observations=(observation("r", BASE, (Outcome.ERRORED,)),),
        ),
    )
    first = prioritize(pair)
    second = prioritize(pair)
    assert [p.test_id for p in first.ranked] == [p.test_id for p in second.ranked]
    assert [p.test_id for p in first.ranked] == ["one", "two"]


def test_the_output_carries_no_probability_field() -> None:
    result = prioritize(
        (
            history(
                observations=(observation("r", BASE, (Outcome.FAILED, Outcome.PASSED)),)
            ),
        )
    )
    for priority in result.ranked:
        assert (
            "score" not in priority.__dict__
            and "probability" not in priority.__dict__
            and "confidence" not in priority.__dict__
        )
        assert priority.flaky_runs == 1 and priority.attempts == 2


def test_the_use_case_composes_with_the_reliability_pipeline() -> None:
    """The reported cutoffs in the output come only from the given rows."""
    hist = history(
        observations=(
            observation("r1", BASE, (Outcome.FAILED, Outcome.PASSED)),
            observation("r2", LATER, (Outcome.PASSED,)),
        )
    )
    result = prioritize((hist,))
    top = result.ranked[0]
    assert top.rank == 1 and top.flaky_runs == 1 and top.attempts == 3
    assert top.last_failure_at == BASE and top.last_run_at == LATER


def test_empty_note_carries_no_counts() -> None:
    note = prioritize(()).note
    assert "0 of 0" not in note  # the empty case states silence, not arithmetic
    assert "model output may be invented" in note
