"""Job payload and backoff rules. No database and no sleeps."""

from datetime import timedelta

import pytest

from flakepilot.orchestration.domain.jobs import (
    InvalidJob,
    backoff,
    error_code,
    gives_up,
    validate_job,
)

ALLOWED = frozenset({"ping"})


def test_payload_accepts_ids_and_tokens() -> None:
    validate_job("ping", {"org_id": "abc-1", "count": 2, "flag": True}, allowed=ALLOWED)


@pytest.mark.parametrize(
    "job_type,payload",
    [
        ("unknown", {}),
        ("ping", {"note": "import os"}),
        ("ping", {"note": "has space"}),
        ("ping", {"nested": {"a": 1}}),
        ("ping", {"items": ["a"]}),
        ("ping", {"code": "def run():\n    pass"}),
    ],
)
def test_payload_rejects_unregistered_types_and_code(
    job_type: str, payload: object
) -> None:
    with pytest.raises(InvalidJob):
        validate_job(job_type, payload, allowed=ALLOWED)  # type: ignore[arg-type]


def test_backoff_is_bounded() -> None:
    assert backoff(1) == timedelta(seconds=1)
    assert backoff(2) == timedelta(seconds=2)
    assert backoff(3) == timedelta(seconds=4)
    assert backoff(8) == timedelta(seconds=60)
    assert gives_up(5, 5, permanent=False)
    assert not gives_up(4, 5, permanent=False)
    assert gives_up(1, 5, permanent=True)


def test_error_codes_cannot_carry_text() -> None:
    assert error_code("handler_error") == "handler_error"
    assert error_code("connection to db.internal failed") == "invalid_error"
