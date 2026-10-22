"""Browser-reproduction rules (ADR-004). Pure values: no I/O and no clock.

The sandbox does not choose the outcome. A runtime failure is never a test
failure, and a JSON field cannot say `verified`, `flaky`, or name a handler.
"""

import json
import re
from dataclasses import dataclass

SCHEDULED = "scheduled"
RUNNING = "running"
COMPLETED = "completed"
CANCELLED = "cancelled"

SUCCESS = "success"
TEST_FAILURE = "test_failure"
INFRA_FAILURE = "infra_failure"
TIMEOUT = "timeout"
INVALID_RESULT = "invalid_result"
INCONCLUSIVE = "inconclusive"

APPROVED_COMMAND = "/opt/flakepilot/reproduce"
JOB_TYPE = "browser_reproduction"

MAX_RESULT_BYTES = 8192
MAX_SCREENSHOT_BYTES = 1_048_576
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_SELECTOR = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_SHA = re.compile(r"[0-9a-f]{40}")
_RESULT_KEYS = frozenset({"status", "selector"})
_COMPLETED = frozenset(
    {SUCCESS, TEST_FAILURE, INFRA_FAILURE, TIMEOUT, INVALID_RESULT, INCONCLUSIVE}
)


class TransitionError(ValueError):
    """The execution is not allowed to move that way."""

    def __init__(self) -> None:
        super().__init__("invalid_transition")


def selector_ok(value: str) -> bool:
    return _SELECTOR.fullmatch(value) is not None and ".." not in value


def sha_ok(value: str) -> bool:
    return _SHA.fullmatch(value) is not None


def screenshot_ok(data: bytes) -> bool:
    return (
        isinstance(data, bytes)
        and data.startswith(PNG_MAGIC)
        and len(data) <= MAX_SCREENSHOT_BYTES
    )


@dataclass(frozen=True)
class Decision:
    """A terminal or in-flight execution the store may persist."""

    state: str
    outcome: str | None
    reason: str | None


def born_scheduled() -> Decision:
    return Decision(SCHEDULED, None, None)


def born_inconclusive(reason: str) -> Decision:
    """A row that never ran. Only the closed-mode and unverified reasons."""
    if reason not in ("execution_closed", "source_unverified"):
        raise TransitionError
    return Decision(COMPLETED, INCONCLUSIVE, reason)


def claim(state: str) -> Decision:
    if state != SCHEDULED:
        raise TransitionError
    return Decision(RUNNING, None, None)


def completion_under_cancel(decision: Decision, *, cancel_requested: bool) -> Decision:
    """A cancel already stored wins over a completion that has not been stored.

    The container label is not an input. A worker lease is not an input.
    Calling this twice is the same as calling it once.
    """
    if not isinstance(cancel_requested, bool):
        raise TransitionError
    if cancel_requested and decision.state != CANCELLED:
        return Decision(CANCELLED, CANCELLED, "cancelled")
    return decision


def finish(state: str, outcome: str, reason: str) -> Decision:
    """A running attempt ends. `inconclusive` is not a finished run."""
    if state != RUNNING:
        raise TransitionError
    if outcome == CANCELLED:
        return Decision(CANCELLED, CANCELLED, "cancelled")
    if outcome not in _COMPLETED or outcome == INCONCLUSIVE:
        raise TransitionError
    return Decision(COMPLETED, outcome, reason)


def classify(
    status: str,
    reason: str,
    result: bytes | None,
    *,
    selector: str,
) -> Decision:
    """Map a runtime report to an outcome. `result` is untrusted.

    `status` comes from our adapter, not from the JSON body. A `closed` report
    is inconclusive even if the body claims a pass.
    """
    if status == "closed":
        return Decision(COMPLETED, INCONCLUSIVE, "execution_closed")
    if status == "timed_out":
        return finish(RUNNING, TIMEOUT, "timeout")
    if status == "cancelled":
        return finish(RUNNING, CANCELLED, "cancelled")
    if status == "infra":
        return finish(RUNNING, INFRA_FAILURE, _reason(reason))
    if status != "finished" or result is None:
        return finish(RUNNING, INVALID_RESULT, "invalid_result")
    parsed = _object(result)
    if parsed is None or set(parsed) != _RESULT_KEYS:
        return finish(RUNNING, INVALID_RESULT, "invalid_result")
    if parsed.get("selector") != selector:
        return finish(RUNNING, INVALID_RESULT, "invalid_result")
    if parsed.get("status") == "passed":
        return finish(RUNNING, SUCCESS, "passed")
    if parsed.get("status") == "failed":
        return finish(RUNNING, TEST_FAILURE, "failed")
    return finish(RUNNING, INVALID_RESULT, "invalid_result")


def _reason(reason: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_]{0,39}", reason):
        return reason
    return "invalid_error"


def _object(data: bytes) -> dict[str, object] | None:
    if len(data) > MAX_RESULT_BYTES:
        return None
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    if type(parsed) is not dict:
        return None
    return parsed
