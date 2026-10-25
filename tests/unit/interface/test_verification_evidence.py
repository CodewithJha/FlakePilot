"""The execution-evidence verifier (`verify-run` / `verify-fetch`).

Regression tests for the 2026-10-25 verification-integrity failure: Render
one-off job `status` was trusted as if it meant exit code 0, but calibration
showed exits 1, 3 and 26 all marked `succeeded`. These tests pin the wrapper:
the recorded exit code is the real one, secrets never survive sanitization,
and the fetch verdict is computed only from the record — never from a Render
status field (which the record does not even carry).
"""

import json
import signal
import sys
from datetime import UTC, datetime

import pytest

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.verification_evidence import (
    evaluate_expectations,
    evidence_line,
    run_target,
    sanitize,
)

# Render one-off job calibration, 2026-10-25 (raw API records, production
# service srv-dbeqd88ae00c73e0hr2g): WHY the wrapper must not trust status.
CALIBRATION = [
    ("job-dbf0k77b9u1s73b75ee0", "import sys; sys.exit(1)", 1, "succeeded"),
    ("job-dbf0k6q9lscs738cqmd0", "import sys; sys.exit(3)", 3, "succeeded"),
    ("job-dbf0k6navr4c73cd56kg", "import sys; sys.exit(26)", 26, "succeeded"),
]


def test_render_job_status_is_not_exit_code_evidence() -> None:
    """Document in code: `succeeded` does not imply exit 0, so never read it."""
    for _, _cmd, true_exit, status in CALIBRATION:
        assert status == "succeeded" and true_exit != 0


def test_run_target_records_true_exit_codes() -> None:
    for true_exit in (0, 1, 3, 26):
        record = run_target(
            [sys.executable, "-c", f"import sys; sys.exit({true_exit})"],
            timeout_seconds=30,
        )
        assert record["exit_code"] == true_exit
        assert record["timed_out"] is False
        assert record["spawn_error"] is None


def test_run_target_records_missing_dependency_as_spawn_class() -> None:
    """A command failing on a missing module is exit 1 with the traceback."""
    record = run_target(
        [sys.executable, "-c", "import definitely_missing_module_xyz"],
        timeout_seconds=30,
    )
    assert record["exit_code"] == 1
    assert record["stderr_tail"] is not None
    assert "ModuleNotFoundError" in record["stderr_tail"]


def test_run_target_records_signals_and_timeout() -> None:
    suicide = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    record = run_target([sys.executable, "-c", suicide], timeout_seconds=30)
    assert record["signal"] == signal.SIGTERM
    assert record["exit_code"] is None

    record = run_target(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.5
    )
    assert record["timed_out"] is True


def test_run_target_keeps_command_identity() -> None:
    record = run_target(
        [sys.executable, "-c", "print('CALIB_MARKER_OK')"], timeout_seconds=30
    )
    assert "CALIB_MARKER_OK" in record["stdout_head"]
    assert record["stdout_sha256"] and len(record["stdout_sha256"]) == 64
    assert record["started_at"] and record["finished_at"]
    start = datetime.fromisoformat(record["started_at"])
    finish = datetime.fromisoformat(record["finished_at"])
    assert finish >= start and (finish - start).total_seconds() < 60
    assert start.tzinfo is UTC


def test_sanitize_redacts_secret_shaped_output() -> None:
    dirty = (
        "FLAKEPILOT_SENTRY_DSN=https://aaaabbbbccccddd@gus.example/1\n"
        "DATABASE_URL=postgres://user:s3cret@db.host/db\n"
        "render_key: rnd_6FLNabcdefghijklmnop\n"
        "Authorization: Bearer bbbbbbbbbbbbbbbbbbbbbbb\n"
        "api_key = 0123456789abcdef\n"
        "plain_value=2026-10-25T13:52:00+00:00 sha256=abc\n"
    )
    clean, count = sanitize(dirty)
    assert count >= 5
    assert "s3cret" not in clean
    assert "aaaabbbbccccddd" not in clean
    assert "rnd_" not in clean
    assert "bbbbb" not in clean
    assert "0123456789abcdef" not in clean
    # non-secret evidence stays readable
    assert "plain_value=2026-10-25T13:52:00+00:00 sha256=abc" in clean
    assert "FLAKEPILOT_SENTRY_DSN=" in clean  # name kept, value redacted


def test_evidence_line_carries_no_secret_values() -> None:
    record = run_target(
        [
            sys.executable,
            "-c",
            "print('DATABASE_URL=postgres://user:s3cret@db.host/db')",
        ],
        timeout_seconds=30,
    )
    line = evidence_line(record)
    assert line.startswith("VERIFY_EVIDENCE ")
    payload = json.loads(line[len("VERIFY_EVIDENCE ") :])
    assert payload["id"] == record["id"]
    assert "endpoint_key" not in payload
    assert "s3cret" not in payload["stdout_head"]
    assert "<redacted>" in payload["stdout_head"]


def test_evidence_record_has_no_render_status_field() -> None:
    """The record's verdict inputs are exit_code/signal/timed_out only."""
    record = run_target([sys.executable, "-c", "pass"], timeout_seconds=30)
    assert "status" not in record
    assert "render_status" not in record


def test_fetch_verdict_uses_record_only() -> None:
    row = {
        "exit_code": 26,
        "signal": None,
        "timed_out": False,
        "stdout_head": "CALIBRATION_EXIT26",
        "stdout_tail": "CALIBRATION_EXIT26",
    }
    assert evaluate_expectations(row, 26, "CALIBRATION_EXIT26")
    assert not evaluate_expectations(row, 0, "CALIBRATION_EXIT26")
    assert not evaluate_expectations(row, 26, "operative_hint_nonexistent")
    assert not evaluate_expectations(
        {"exit_code": None, "timed_out": False, "stdout_head": "", "stdout_tail": ""},
        0,
        None,
    )


def test_run_verify_run_requires_target_and_exits_with_target_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from flakepilot.interface.composition_root.verification_evidence import (
        run_verify_run,
    )

    settings = Settings(Secret("postgres://local/db"))
    assert run_verify_run(settings, ["--"]) == 2
    capsys.readouterr()

    target = [
        "--",
        sys.executable,
        "-c",
        "print('CAL26_GONE'); import sys; sys.exit(26)",
    ]
    code = run_verify_run(settings, target)  # DB unreachable: evidence still prints
    out, _ = capsys.readouterr()
    evidence = [ln for ln in out.splitlines() if ln.startswith("VERIFY_EVIDENCE ")][-1]
    payload = json.loads(evidence[len("VERIFY_EVIDENCE ") :])
    assert payload["exit_code"] == 26
    assert payload["db_write"].startswith("failed:")
    assert code == 26
