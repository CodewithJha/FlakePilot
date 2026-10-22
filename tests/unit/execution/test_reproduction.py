"""Outcome rules. A sandbox failure is not a test failure."""

import json

import pytest

from flakepilot.execution.domain.reproduction import (
    APPROVED_COMMAND,
    CANCELLED,
    COMPLETED,
    INFRA_FAILURE,
    INVALID_RESULT,
    RUNNING,
    SCHEDULED,
    SUCCESS,
    TEST_FAILURE,
    TIMEOUT,
    Decision,
    TransitionError,
    born_inconclusive,
    born_scheduled,
    claim,
    classify,
    completion_under_cancel,
    finish,
    screenshot_ok,
    selector_ok,
    sha_ok,
)

SHA = "a" * 40
SELECTOR = "tests.test_login.TestLogin.test_ok"


def _result(status: str, **extra: object) -> bytes:
    body: dict[str, object] = {"status": status, "selector": SELECTOR}
    body.update(extra)
    return json.dumps(body).encode()


def test_selector_and_sha_reject_traversal_and_uppercase() -> None:
    assert selector_ok(SELECTOR)
    assert not selector_ok("../etc/passwd")
    assert not selector_ok("a b")
    assert sha_ok(SHA)
    assert not sha_ok("A" * 40)
    assert not sha_ok("abc")
    assert APPROVED_COMMAND == "/opt/flakepilot/reproduce"


def test_a_committed_cancel_replaces_a_completion_that_is_not_stored() -> None:
    success = finish(RUNNING, SUCCESS, "passed")
    stored = completion_under_cancel(success, cancel_requested=True)
    assert stored == Decision(CANCELLED, CANCELLED, "cancelled")
    assert completion_under_cancel(success, cancel_requested=False) == success
    assert completion_under_cancel(stored, cancel_requested=True) == stored


def test_scheduled_claim_and_a_pass_are_the_only_happy_path() -> None:
    assert born_scheduled() == Decision(SCHEDULED, None, None)
    assert claim(SCHEDULED) == Decision(RUNNING, None, None)
    with pytest.raises(TransitionError):
        claim(COMPLETED)
    decision = classify("finished", "ignored", _result("passed"), selector=SELECTOR)
    assert decision == Decision(COMPLETED, SUCCESS, "passed")
    assert finish(RUNNING, SUCCESS, "passed") == decision


def test_failures_stay_in_their_classes() -> None:
    assert classify("finished", "x", _result("failed"), selector=SELECTOR) == Decision(
        COMPLETED, TEST_FAILURE, "failed"
    )
    assert classify("infra", "runtime_unavailable", None, selector=SELECTOR) == (
        Decision(COMPLETED, INFRA_FAILURE, "runtime_unavailable")
    )
    assert classify("timed_out", "nope", _result("passed"), selector=SELECTOR) == (
        Decision(COMPLETED, TIMEOUT, "timeout")
    )
    assert classify("cancelled", "nope", None, selector=SELECTOR) == Decision(
        CANCELLED, CANCELLED, "cancelled"
    )
    assert classify("closed", "success", _result("passed"), selector=SELECTOR) == (
        Decision(COMPLETED, "inconclusive", "execution_closed")
    )


def test_output_cannot_claim_verification_or_name_a_handler() -> None:
    for extra in (
        {"verified": True},
        {"flaky": True},
        {"outcome": "success"},
        {"handler": "browser_reproduction"},
        {"tenant": "other"},
    ):
        decision = classify(
            "finished", "x", _result("passed", **extra), selector=SELECTOR
        )
        assert decision.outcome == INVALID_RESULT
    mismatch = classify(
        "finished",
        "x",
        _result("passed").replace(SELECTOR.encode(), b"other"),
        selector=SELECTOR,
    )
    assert mismatch.outcome == INVALID_RESULT
    assert classify("finished", "x", b"not-json", selector=SELECTOR).outcome == (
        INVALID_RESULT
    )
    assert born_inconclusive("source_unverified").outcome == "inconclusive"
    with pytest.raises(TransitionError):
        born_inconclusive("passed")
    with pytest.raises(TransitionError):
        finish(SCHEDULED, SUCCESS, "passed")


def test_screenshot_is_a_bounded_png() -> None:
    assert screenshot_ok(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
    assert not screenshot_ok(b"GIF89a")
    assert not screenshot_ok(b"\x89PNG\r\n\x1a\n" + b"x" * 2_000_000)
