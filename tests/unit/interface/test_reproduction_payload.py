"""A job payload cannot choose a tenant, a command, or an extra field."""

from datetime import UTC, datetime

import pytest

from flakepilot.interface.composition_root.reproduction import _execution_id
from flakepilot.orchestration.application.jobs import Job, Payload, PermanentJob

NOW = datetime(2026, 10, 21, tzinfo=UTC)
EXECUTION = "11111111-1111-1111-1111-111111111111"


def _job(payload: Payload) -> Job:
    return Job(
        id="22222222-2222-2222-2222-222222222222",
        job_type="browser_reproduction",
        payload_version=1,
        payload=payload,
        state="running",
        run_at=NOW,
        attempts=1,
        max_attempts=5,
        lease_owner="worker-a",
        lease_expires_at=NOW,
        last_error=None,
        correlation_id="corr-1",
        causation_id=None,
        cancel_requested=False,
    )


def test_only_an_execution_id_is_accepted() -> None:
    assert _execution_id(_job({"execution_id": EXECUTION})) == EXECUTION
    for payload in (
        {"execution_id": EXECUTION, "org_id": "other"},
        {"execution_id": EXECUTION, "command": "/bin/sh"},
        {},
        {"execution_id": 1},
    ):
        with pytest.raises(PermanentJob):
            _execution_id(_job(payload))
