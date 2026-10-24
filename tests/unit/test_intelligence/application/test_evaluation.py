"""Leakage-safe evaluation harness: split, metrics, baseline, predictor outcomes.

Every number asserted here is computed by hand from fixtures in this file;
none is an evaluation result on real history.
"""

import asyncio
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest

from flakepilot.test_intelligence.application.evaluation import (
    BASELINE,
    Evaluation,
    Feature,
    Prediction,
    Status,
    brier,
    build_examples,
    evaluate,
    precision_at_k,
    roc_auc,
    temporal_split,
)
from flakepilot.test_intelligence.application.prioritization import prioritize
from flakepilot.test_intelligence.application.store import IdentityHistory
from flakepilot.test_intelligence.domain.classification import Observation
from flakepilot.test_intelligence.domain.results import Outcome, RunSource

T0 = datetime(2026, 10, 1, tzinfo=UTC)
WEEK = timedelta(days=7)
F, P = Outcome.FAILED, Outcome.PASSED


def obs(run: str, at: datetime, *attempts: Outcome, env: str = "linux") -> Observation:
    return Observation(
        run_id=run,
        commit_sha="a" * 40,
        environment=env,
        received_at=at,
        source=RunSource.CI_UPLOAD,
        attempts=attempts,
    )


def ident(test_id: str, *observations: Observation) -> IdentityHistory:
    return IdentityHistory(test_id, "tests.x", test_id, observations)


def fixture() -> tuple[IdentityHistory, ...]:
    """Four identities with runs in the past, the train label week, and the
    test label week, so both splits have both classes."""
    d = timedelta(days=1)
    return (
        ident(
            "a", obs("1", T0 - d, F, P), obs("5", T0 + d, F), obs("9", T0 + 8 * d, F)
        ),
        ident("b", obs("1", T0 - d, P), obs("5", T0 + d, P), obs("9", T0 + 8 * d, P)),
        ident(
            "c",
            obs("2", T0 - 2 * d, F),
            obs("6", T0 + 2 * d, P),
            obs("9", T0 + 8 * d, F),
        ),
        ident(
            "d",
            obs("2", T0 - 2 * d, P),
            obs("6", T0 + 2 * d, F),
            obs("9", T0 + 8 * d, P),
        ),
    )


def test_features_never_see_rows_after_the_cutoff() -> None:
    d = timedelta(days=1)
    past = obs("1", T0 - d, P)
    calm = ident("a", past, obs("2", T0 + d, P))
    stormy = ident("a", past, obs("2", T0 + d, F, F, F), obs("3", T0 + 2 * d, F))
    (quiet,) = build_examples((calm,), T0, WEEK)
    (loud,) = build_examples((stormy,), T0, WEEK)
    assert quiet.features == loud.features  # only the future differs
    assert (quiet.label, loud.label) == (False, True)


def test_cutoff_boundaries_and_unobserved_labels() -> None:
    d = timedelta(days=1)
    examples = build_examples(
        (
            ident("at_cutoff", obs("1", T0, F), obs("2", T0 + WEEK, F)),
            ident("past_only", obs("1", T0 - d, F)),
            ident("future_only", obs("2", T0 + d, F)),
            ident("beyond", obs("1", T0 - d, P), obs("3", T0 + WEEK + d, F)),
        ),
        T0,
        WEEK,
    )
    # A run at the cutoff is past; a run at cutoff + horizon is in the label window.
    # Identities without both a past run and a label-window run are not examples.
    assert [(e.test_id, e.features[0], e.label) for e in examples] == [
        ("at_cutoff", 1.0, True)
    ]


def test_training_never_depends_on_rows_after_the_test_cutoff() -> None:
    histories = fixture()
    test_cutoff = T0 + WEEK
    truncated = tuple(
        ident(
            h.test_id,
            *(o for o in h.observations if o.received_at <= test_cutoff),
        )
        for h in histories
    )
    full_train, test = temporal_split(histories, T0, WEEK)
    cut_train, _ = temporal_split(truncated, T0, WEEK)
    assert full_train == cut_train
    assert [e.label for e in full_train] == [True, False, False, True]
    assert [e.label for e in test] == [True, False, True, False]


def test_feature_values_are_counts_from_the_past() -> None:
    d = timedelta(days=1)
    (example,) = build_examples(
        (
            ident(
                "a",
                obs("1", T0 - 3 * d, F, P, env="linux"),
                obs("2", T0 - d, F, env="macos"),
                obs("3", T0 + d, P),
            ),
        ),
        T0,
        WEEK,
    )
    assert example.features == (2.0, 3.0, 2.0, 2.0, 1.0, 2.0, 24.0, 24.0)


def test_a_test_that_never_failed_has_a_missing_recency_feature() -> None:
    (example,) = build_examples(
        (ident("a", obs("1", T0, P), obs("2", T0 + WEEK, P)),), T0, WEEK
    )
    assert example.features[6] is None and example.features[7] == 0.0


def test_baseline_score_is_the_shipped_rank_and_order_independent() -> None:
    histories = fixture()
    forward = build_examples(histories, T0, WEEK)
    backward = build_examples(tuple(reversed(histories)), T0, WEEK)
    assert forward == backward
    cut = tuple(
        ident(
            h.test_id,
            *(o for o in h.observations if o.received_at <= T0),
        )
        for h in histories
    )
    rank = {p.test_id: p.rank for p in prioritize(cut).ranked}
    assert {e.test_id: -e.baseline_score for e in forward} == rank


def test_precision_at_k() -> None:
    scores, labels = [0.9, 0.8, 0.1, 0.4], [True, False, True, False]
    assert precision_at_k(scores, labels, 1) == 1.0
    assert precision_at_k(scores, labels, 2) == 0.5
    assert precision_at_k(scores, labels, 10) == 0.5  # k beyond n uses all rows
    with pytest.raises(ValueError):
        precision_at_k([], [], 1)


def test_roc_auc_matches_the_hand_count() -> None:
    # Pairs (pos, neg): (0.35>0.1) (0.35<0.4) (0.8>0.1) (0.8>0.4) -> 3/4.
    assert roc_auc([0.1, 0.4, 0.35, 0.8], [False, False, True, True]) == 0.75
    assert roc_auc([1.0, 1.0], [True, False]) == 0.5  # a tie counts half
    assert roc_auc([0.2, 0.9], [True, True]) is None  # one class: undefined


def test_brier_score() -> None:
    assert math.isclose(brier([1.0, 0.0, 0.5], [True, False, True]), 0.25 / 3)
    with pytest.raises(ValueError):
        brier([0.5], [True, False])


class FixedPredictor:
    def __init__(self, answer: Prediction) -> None:
        self.answer = answer
        self.seen: list[object] = []

    async def predict(
        self,
        train_features: Sequence[tuple[Feature, ...]],
        train_labels: Sequence[bool],
        test_features: Sequence[tuple[Feature, ...]],
    ) -> Prediction:
        self.seen = [train_features, train_labels, test_features]
        return self.answer


def test_without_a_predictor_the_baseline_runs_and_the_candidate_is_blocked() -> None:
    report = asyncio.run(evaluate(fixture(), train_cutoff=T0, horizon=WEEK, k=2))
    card = report.dataset
    assert (
        card.train_rows,
        card.train_positives,
        card.test_rows,
        card.test_positives,
    ) == (
        4,
        2,
        4,
        2,
    )
    assert card.test_cutoff == T0 + WEEK and len(card.fingerprint) == 64
    assert report.baseline.name == BASELINE
    assert report.baseline.status is Status.MEASURED and report.baseline.brier is None
    assert report.base_rate.brier == 0.25  # constant 0.5 against 2 of 4 positives
    assert report.base_rate.roc_auc is None and report.base_rate.precision_at_k is None
    assert report.candidate.status is Status.BLOCKED
    assert report.candidate.reason == "not_configured"
    assert report.candidate.roc_auc is None


def test_a_valid_prediction_is_measured_and_sees_only_features() -> None:
    predictor = FixedPredictor(Prediction(True, (0.9, 0.1, 0.8, 0.2), None, "fake-1"))
    report = asyncio.run(
        evaluate(fixture(), train_cutoff=T0, horizon=WEEK, k=2, predictor=predictor)
    )
    assert report.candidate.name == "tabpfn:fake-1"
    assert report.candidate.status is Status.MEASURED
    assert report.candidate.precision_at_k == 1.0 and report.candidate.roc_auc == 1.0
    assert report.candidate.brier is not None
    assert math.isclose(report.candidate.brier, (0.01 + 0.01 + 0.04 + 0.04) / 4)
    train_features, train_labels, test_features = predictor.seen
    flat = [v for row in [*train_features, *test_features] for v in row]  # type: ignore[misc]
    assert all(v is None or isinstance(v, float) for v in flat)
    assert list(train_labels) == [True, False, False, True]  # type: ignore[call-overload]


@pytest.mark.parametrize(
    "probabilities", [(0.5,), (0.1, 0.2, 1.5, 0.3), (0.1, math.nan, 0.2, 0.3), None]
)
def test_an_unusable_prediction_fails_without_numbers(
    probabilities: tuple[float, ...] | None,
) -> None:
    predictor = FixedPredictor(Prediction(True, probabilities, None, "fake-1"))
    report = asyncio.run(
        evaluate(fixture(), train_cutoff=T0, horizon=WEEK, predictor=predictor)
    )
    assert report.candidate.status is Status.FAILED
    assert report.candidate.reason == "bad_prediction"
    assert report.candidate.brier is None


def test_an_unavailable_predictor_is_blocked_with_its_reason() -> None:
    predictor = FixedPredictor(Prediction(False, None, "provider_unauthorized", "v3"))
    report = asyncio.run(
        evaluate(fixture(), train_cutoff=T0, horizon=WEEK, predictor=predictor)
    )
    assert report.candidate.status is Status.BLOCKED
    assert report.candidate.reason == "provider_unauthorized"


def test_single_class_training_and_empty_test_never_call_the_predictor() -> None:
    green = tuple(
        ident(t, obs("1", T0, P), obs("2", T0 + WEEK, P), obs("3", T0 + 2 * WEEK, P))
        for t in ("a", "b")
    )
    predictor = FixedPredictor(Prediction(True, (0.5, 0.5), None, "fake-1"))
    report = asyncio.run(
        evaluate(green, train_cutoff=T0, horizon=WEEK, predictor=predictor)
    )
    assert report.candidate.reason == "single_class_train" and predictor.seen == []
    empty = asyncio.run(
        evaluate((), train_cutoff=T0, horizon=WEEK, predictor=predictor)
    )
    assert empty.baseline.status is Status.BLOCKED
    assert empty.candidate.reason == "no_test_examples" and predictor.seen == []


def test_the_use_case_reads_through_the_end_of_the_test_window() -> None:
    seen: dict[str, object] = {}

    class Store:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def repo_history(self, **kwargs: object) -> tuple[IdentityHistory, ...]:
            seen.update(kwargs)
            return fixture()

    report = asyncio.run(
        Evaluation(lambda: Store(), max_tests=50).run(  # type: ignore[arg-type,return-value]
            org_id="o", repo_id="r", train_cutoff=T0, horizon=WEEK
        )
    )
    assert seen == {
        "org_id": "o",
        "repo_id": "r",
        "cutoff": T0 + 2 * WEEK,
        "max_tests": 50,
    }
    assert report.dataset.test_rows == 4
