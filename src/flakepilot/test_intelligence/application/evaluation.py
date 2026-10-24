"""Leakage-safe evaluation of failure prediction against the baseline (goal 12).

The question a probabilistic model (TabPFN or similar) must answer better than
the deterministic baseline before it could ever be selected:

    Target `fails_within_horizon`: does a test identity have a failing or
    errored attempt in a run received in `(cutoff, cutoff + horizon]`?

Rules, each enforced here and covered by tests:

- **Features from the past only.** Every feature is computed from runs
  received at or before the example's cutoff; label runs never feed features.
- **Temporal split.** Training examples are cut at `train_cutoff` and labelled
  from `(train_cutoff, test_cutoff]`; test examples are cut at
  `test_cutoff = train_cutoff + horizon` and labelled from the horizon after.
  Nothing a model is trained on comes after `test_cutoff`.
- **Observed labels only.** An identity without a run in the label window has
  no observed outcome and is not an example (no "silent pass" invented).
- **Baseline unchanged.** The baseline score is the existing
  `prioritize()` rank over the same cutoff-bounded history, so the comparison
  is against what the product actually ships.
- **No invented numbers.** Metrics are computed by the stdlib code below from
  the examples; a predictor that is absent or fails is reported as such with a
  reason code, never filled in.

Classification authority stays with `ti-classification/1`: this module scores
attention order only and never issues verdicts.
"""

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from flakepilot.test_intelligence.application.prioritization import prioritize
from flakepilot.test_intelligence.application.store import IdentityHistory, TiStore
from flakepilot.test_intelligence.domain.classification import Observation
from flakepilot.test_intelligence.domain.results import Outcome

UnitOfWork = Callable[[], AbstractAsyncContextManager[TiStore]]

TARGET = "fails_within_horizon"
FEATURE_SCHEMA = "ti-eval-features/1"
FEATURES = (
    "runs",
    "attempts",
    "failed_attempts",
    "failed_runs",
    "flaky_runs",
    "environments",
    "hours_since_last_failure",  # missing when the identity never failed
    "hours_since_last_run",
)
BASELINE = "ti-prioritization-baseline"

Feature = float | None


@dataclass(frozen=True)
class Example:
    """One identity at one cutoff. `baseline_score` is the negated baseline
    rank (higher means look first); it is never a model input."""

    test_id: str
    features: tuple[Feature, ...]
    baseline_score: float
    label: bool


@dataclass(frozen=True)
class Prediction:
    """A predictor's answer: one probability of the positive class per test
    row, or a short reason code. `model` names the exact model version."""

    available: bool
    probabilities: tuple[float, ...] | None
    reason: str | None
    model: str


class Predictor(Protocol):
    """A probabilistic candidate. Receives only feature rows and training
    labels — never test identities, names, or tenant IDs."""

    async def predict(
        self,
        train_features: Sequence[tuple[Feature, ...]],
        train_labels: Sequence[bool],
        test_features: Sequence[tuple[Feature, ...]],
    ) -> Prediction: ...


class Status(StrEnum):
    MEASURED = "measured"
    BLOCKED = "blocked"  # nothing to run with: no predictor, too little data
    FAILED = "failed"  # the predictor ran and its answer was unusable


@dataclass(frozen=True)
class ModelResult:
    name: str
    status: Status
    reason: str | None
    precision_at_k: float | None
    roc_auc: float | None
    brier: float | None  # only for probability outputs; a rank has none


@dataclass(frozen=True)
class DatasetCard:
    """What was evaluated, precisely enough to reproduce or refuse a claim."""

    target: str
    feature_schema: str
    features: tuple[str, ...]
    train_cutoff: datetime
    test_cutoff: datetime
    horizon: timedelta
    train_rows: int
    train_positives: int
    test_rows: int
    test_positives: int
    k: int
    fingerprint: str  # SHA-256 over the canonical examples


@dataclass(frozen=True)
class EvaluationReport:
    dataset: DatasetCard
    baseline: ModelResult
    base_rate: ModelResult  # constant training prevalence: the calibration floor
    candidate: ModelResult


def _failed(o: Observation) -> bool:
    return any(attempt.failing for attempt in o.attempts)


def _flaky(o: Observation) -> bool:
    seen_failure = False
    for attempt in o.attempts:
        if attempt.failing:
            seen_failure = True
        elif seen_failure and attempt == Outcome.PASSED:
            return True
    return False


def _hours(since: datetime | None, cutoff: datetime) -> Feature:
    return None if since is None else (cutoff - since).total_seconds() / 3600


def _features(past: tuple[Observation, ...], cutoff: datetime) -> tuple[Feature, ...]:
    failures = [o.received_at for o in past if _failed(o)]
    return (
        float(len(past)),
        float(sum(len(o.attempts) for o in past)),
        float(sum(1 for o in past for a in o.attempts if a.failing)),
        float(sum(1 for o in past if _failed(o))),
        float(sum(1 for o in past if _flaky(o))),
        float(len({o.environment for o in past})),
        _hours(max(failures, default=None), cutoff),
        _hours(max(o.received_at for o in past), cutoff),
    )


def build_examples(
    histories: Sequence[IdentityHistory], cutoff: datetime, horizon: timedelta
) -> tuple[Example, ...]:
    """Examples at one cutoff, sorted by test ID."""
    if horizon <= timedelta(0):
        raise ValueError("the horizon is longer than zero")
    end = cutoff + horizon
    past_by_test: dict[str, tuple[Observation, ...]] = {}
    labels: dict[str, bool] = {}
    for history in histories:
        past = tuple(o for o in history.observations if o.received_at <= cutoff)
        future = [o for o in history.observations if cutoff < o.received_at <= end]
        if past and future:
            past_by_test[history.test_id] = past
            labels[history.test_id] = any(_failed(o) for o in future)
    ranking = prioritize(
        tuple(
            IdentityHistory(h.test_id, h.classname, h.name, past_by_test[h.test_id])
            for h in histories
            if h.test_id in past_by_test
        )
    )
    rank = {p.test_id: p.rank for p in ranking.ranked}
    return tuple(
        Example(
            test_id=test_id,
            features=_features(past_by_test[test_id], cutoff),
            baseline_score=-float(rank[test_id]),
            label=labels[test_id],
        )
        for test_id in sorted(past_by_test)
    )


def temporal_split(
    histories: Sequence[IdentityHistory], train_cutoff: datetime, horizon: timedelta
) -> tuple[tuple[Example, ...], tuple[Example, ...]]:
    """(train, test): train labels end exactly where test features end."""
    return (
        build_examples(histories, train_cutoff, horizon),
        build_examples(histories, train_cutoff + horizon, horizon),
    )


def precision_at_k(scores: Sequence[float], labels: Sequence[bool], k: int) -> float:
    """Share of positives among the k highest scores; ties keep input order."""
    if not scores or k < 1:
        raise ValueError("precision@k needs at least one row and k >= 1")
    order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
    return sum(labels[i] for i in order) / len(order)


def roc_auc(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    """Mann-Whitney AUC with average ranks for ties; None with one class."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for position in range(i, j + 1):
            ranks[order[position]] = (i + j) / 2 + 1
        i = j + 1
    positive_ranks = sum(r for r, label in zip(ranks, labels, strict=True) if label)
    return (positive_ranks - positives * (positives + 1) / 2) / (positives * negatives)


def brier(probabilities: Sequence[float], labels: Sequence[bool]) -> float:
    if not probabilities or len(probabilities) != len(labels):
        raise ValueError("Brier needs one probability per label")
    return sum(
        (p - float(y)) ** 2 for p, y in zip(probabilities, labels, strict=True)
    ) / len(labels)


def _fingerprint(train: Sequence[Example], test: Sequence[Example]) -> str:
    canonical = json.dumps(
        [
            [[e.test_id, list(e.features), e.label] for e in train],
            [[e.test_id, list(e.features), e.label] for e in test],
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _blocked(name: str, reason: str) -> ModelResult:
    return ModelResult(name, Status.BLOCKED, reason, None, None, None)


def _measured(
    name: str,
    scores: Sequence[float],
    labels: Sequence[bool],
    k: int,
    probabilities: Sequence[float] | None,
) -> ModelResult:
    return ModelResult(
        name=name,
        status=Status.MEASURED,
        reason=None,
        precision_at_k=precision_at_k(scores, labels, k),
        roc_auc=roc_auc(scores, labels),
        brier=None if probabilities is None else brier(probabilities, labels),
    )


async def evaluate(
    histories: Sequence[IdentityHistory],
    *,
    train_cutoff: datetime,
    horizon: timedelta,
    k: int = 10,
    predictor: Predictor | None = None,
    candidate: str = "tabpfn",
) -> EvaluationReport:
    """Split, score the baseline, the base rate, and the candidate if any."""
    if k < 1:
        raise ValueError("k is at least 1")
    train, test = temporal_split(histories, train_cutoff, horizon)
    labels = [e.label for e in test]
    card = DatasetCard(
        target=TARGET,
        feature_schema=FEATURE_SCHEMA,
        features=FEATURES,
        train_cutoff=train_cutoff,
        test_cutoff=train_cutoff + horizon,
        horizon=horizon,
        train_rows=len(train),
        train_positives=sum(e.label for e in train),
        test_rows=len(test),
        test_positives=sum(labels),
        k=k,
        fingerprint=_fingerprint(train, test),
    )
    if not test:
        reason = "no_test_examples"
        return EvaluationReport(
            card,
            _blocked(BASELINE, reason),
            _blocked("base_rate", reason),
            _blocked(candidate, reason),
        )
    baseline = _measured(BASELINE, [e.baseline_score for e in test], labels, k, None)
    if not train:
        reason = "no_train_examples"
        return EvaluationReport(
            card, baseline, _blocked("base_rate", reason), _blocked(candidate, reason)
        )
    prevalence = card.train_positives / card.train_rows
    # A constant ranks nothing: only its Brier score (the calibration floor) means anything.
    base_rate = ModelResult(
        "base_rate",
        Status.MEASURED,
        None,
        None,
        None,
        brier([prevalence] * len(test), labels),
    )
    if predictor is None:
        return EvaluationReport(
            card, baseline, base_rate, _blocked(candidate, "not_configured")
        )
    if card.train_positives in (0, card.train_rows):
        return EvaluationReport(
            card, baseline, base_rate, _blocked(candidate, "single_class_train")
        )
    answer = await predictor.predict(
        [e.features for e in train],
        [e.label for e in train],
        [e.features for e in test],
    )
    name = f"{candidate}:{answer.model}"
    if not answer.available:
        return EvaluationReport(
            card, baseline, base_rate, _blocked(name, answer.reason or "unavailable")
        )
    probabilities = answer.probabilities
    if (
        probabilities is None
        or len(probabilities) != len(test)
        or not all(math.isfinite(p) and 0.0 <= p <= 1.0 for p in probabilities)
    ):
        return EvaluationReport(
            card,
            baseline,
            base_rate,
            ModelResult(name, Status.FAILED, "bad_prediction", None, None, None),
        )
    return EvaluationReport(
        card,
        baseline,
        base_rate,
        _measured(name, probabilities, labels, k, probabilities),
    )


class Evaluation:
    """Read the pair's stored history through the test window's end, then
    evaluate. One read; the split happens in memory, by receipt time."""

    def __init__(self, unit_of_work: UnitOfWork, max_tests: int = 1000) -> None:
        self._unit_of_work = unit_of_work
        self._max_tests = max_tests

    async def run(
        self,
        *,
        org_id: str,
        repo_id: str,
        train_cutoff: datetime,
        horizon: timedelta,
        k: int = 10,
        predictor: Predictor | None = None,
    ) -> EvaluationReport:
        async with self._unit_of_work() as store:
            histories = await store.repo_history(
                org_id=org_id,
                repo_id=repo_id,
                cutoff=train_cutoff + 2 * horizon,
                max_tests=self._max_tests,
            )
        return await evaluate(
            histories,
            train_cutoff=train_cutoff,
            horizon=horizon,
            k=k,
            predictor=predictor,
        )
