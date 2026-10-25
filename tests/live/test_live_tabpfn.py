"""One real TabPFN fit and predict on synthetic numbers (goal 12). Owner-run only.

Skipped unless the key is in the environment, so the default suite and CI send
nothing. A skip is not verification. Running it spends one fit and one predict
on 20 synthetic training rows and 5 test rows (no FlakePilot data), plus one
refused request with an invalid key:

    FLAKEPILOT_LIVE_TABPFN_API_KEY=... uv run pytest tests/live -k tabpfn

The live key uses the `FLAKEPILOT_LIVE_` prefix like every other live test, so
a deployment's `FLAKEPILOT_TABPFN_API_KEY` is never picked up by a test run.
Plumbing only: a separable toy set says nothing about prioritization quality.
"""

import asyncio
import os

import pytest

from flakepilot.integrations.infrastructure.hosted_http import call
from flakepilot.test_intelligence.infrastructure.tabpfn import TabPfnPredictor

KEY = os.environ.get("FLAKEPILOT_LIVE_TABPFN_API_KEY", "")

TRAIN = [(float(i), float(i % 3)) for i in range(10)] + [
    (float(i), float(i % 3)) for i in range(30, 40)
]
LABELS = [False] * 10 + [True] * 10
TEST = [(1.5, 0.0), (4.0, 1.0), (8.0, 2.0), (33.0, 0.0), (38.5, 1.0)]


@pytest.mark.skipif(not KEY, reason="FLAKEPILOT_LIVE_TABPFN_API_KEY is not set")
def test_live_tabpfn_predicts_probabilities_for_every_test_row() -> None:
    prediction = asyncio.run(
        TabPfnPredictor(api_key=KEY, transport=call, timeout_seconds=300).predict(
            TRAIN, LABELS, TEST
        )
    )
    assert prediction.available, prediction.reason
    assert prediction.probabilities is not None
    assert len(prediction.probabilities) == len(TEST)
    assert all(0.0 <= p <= 1.0 for p in prediction.probabilities)
    assert max(prediction.probabilities[:3]) < min(prediction.probabilities[3:])
    assert prediction.model is not None
    assert prediction.model.startswith("v3.5_default@")


@pytest.mark.skipif(not KEY, reason="FLAKEPILOT_LIVE_TABPFN_API_KEY is not set")
def test_live_tabpfn_invalid_key_is_unauthorized() -> None:
    prediction = asyncio.run(
        TabPfnPredictor(api_key="invalid-key", transport=call).predict(
            TRAIN, LABELS, TEST
        )
    )
    assert (prediction.available, prediction.reason) == (
        False,
        "provider_unauthorized",
    )
