"""Evidence eligibility. A fixture attestation is not produced by this deployment."""

import uuid

import pytest

from flakepilot.execution.application.reproduction import (
    Execution,
    UnavailableIsolation,
    evidence_of,
)
from flakepilot.execution.domain.evidence import (
    EvidenceVerdict,
    IsolationChecks,
    accept,
    unavailable,
)

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SHA = "ab" * 20


def _checks(**overrides: bool) -> IsolationChecks:
    values = {
        "network_denied": True,
        "metadata_denied": True,
        "host_mounts_denied": True,
        "credentials_absent": True,
        "unprivileged": True,
        "limits_enforced": True,
        "cleanup_verified": True,
    }
    values.update(overrides)
    return IsolationChecks(**values)


def _verdict(
    outcome: str | None,
    *,
    state: str = "completed",
    reason: str | None = None,
    sha: str = SHA,
    org_id: str = ORG,
    repo_id: str = REPO,
    evidence_partial: bool = False,
    superseded: bool = False,
    checks: IsolationChecks | None = None,
) -> EvidenceVerdict:
    return accept(
        outcome=outcome,
        state=state,
        reason=reason,
        sha=sha,
        org_id=org_id,
        repo_id=repo_id,
        evidence_partial=evidence_partial,
        superseded=superseded,
        checks=unavailable() if checks is None else checks,
    )


def test_unavailable_checks_never_pass() -> None:
    checks = unavailable()
    assert checks.passed() is False
    assert UnavailableIsolation().current() == checks
    assert all(
        value is False
        for value in (
            checks.network_denied,
            checks.metadata_denied,
            checks.host_mounts_denied,
            checks.credentials_absent,
            checks.unprivileged,
            checks.limits_enforced,
            checks.cleanup_verified,
        )
    )


def test_a_reviewed_pass_or_failure_can_be_eligible() -> None:
    """The true checks are a test fixture. Production uses `unavailable()`."""
    passed = _verdict("success", reason="passed", checks=_checks())
    failed = _verdict("test_failure", reason="failed", checks=_checks())
    assert passed == EvidenceVerdict("eligible", "success")
    assert failed == EvidenceVerdict("eligible", "test_failure")
    assert "flaky" not in passed.reason
    assert "flaky" not in failed.reason


def test_one_failure_is_not_a_flakiness_claim() -> None:
    verdict = _verdict("test_failure", reason="failed", checks=_checks())
    assert verdict.reason == "test_failure"


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        ("inconclusive", "execution_closed", "execution_closed"),
        ("inconclusive", "source_unverified", "source_unverified"),
        ("cancelled", "cancelled", "cancelled"),
        ("timeout", "timeout", "timeout"),
        ("infra_failure", "runtime_unavailable", "infra_failure"),
        ("invalid_result", "invalid_result", "invalid_result"),
        ("inconclusive", "other", "inconclusive"),
        ("success", "passed", "isolation_unverified"),
        ("test_failure", "failed", "isolation_unverified"),
    ],
)
def test_unproven_results_stay_ineligible(
    outcome: str, reason: str, expected: str
) -> None:
    state = "cancelled" if outcome == "cancelled" else "completed"
    verdict = _verdict(outcome, state=state, reason=reason)
    assert verdict == EvidenceVerdict("ineligible", expected)


def test_partial_bad_sha_tenant_and_superseded_stay_ineligible() -> None:
    proven = _checks()
    assert _verdict(
        "success", reason="passed", evidence_partial=True, checks=proven
    ) == EvidenceVerdict("ineligible", "incomplete")
    assert _verdict("success", reason="passed", sha="abc", checks=proven) == (
        EvidenceVerdict("ineligible", "source_unverified")
    )
    assert _verdict("success", reason="passed", org_id="nope", checks=proven) == (
        EvidenceVerdict("ineligible", "tenant")
    )
    assert _verdict("success", reason="passed", superseded=True, checks=proven) == (
        EvidenceVerdict("ineligible", "superseded")
    )
    assert _verdict(
        "success", reason="passed", state="running", checks=proven
    ) == EvidenceVerdict("ineligible", "inconclusive")
    assert _verdict(
        "success", reason="passed", checks=_checks(network_denied=False)
    ) == EvidenceVerdict("ineligible", "isolation_unverified")


def test_a_row_without_a_provider_is_not_eligible() -> None:
    record = Execution(
        {
            "id": str(uuid.uuid4()),
            "org_id": ORG,
            "repo_id": REPO,
            "selector": "tests.login.TestLogin.test_ok",
            "sha": SHA,
            "state": "completed",
            "outcome": "success",
            "reason": "passed",
            "generation": 1,
            "job_id": None,
            "evidence_partial": False,
            "cancel_requested": False,
            "lease_owner": None,
            "lease_expires_at": None,
            "correlation_id": "corr-evidence",
        }
    )
    assert evidence_of(record) == EvidenceVerdict("ineligible", "isolation_unverified")


class _Proven:
    def current(self) -> IsolationChecks:
        return _checks()


def test_a_provider_is_explicit_and_still_not_the_default() -> None:
    record = Execution(
        {
            "id": str(uuid.uuid4()),
            "org_id": ORG,
            "repo_id": REPO,
            "selector": "tests.login.TestLogin.test_ok",
            "sha": SHA,
            "state": "completed",
            "outcome": "success",
            "reason": "passed",
            "generation": 1,
            "job_id": None,
            "evidence_partial": False,
            "cancel_requested": False,
            "lease_owner": None,
            "lease_expires_at": None,
            "correlation_id": "corr-proven",
        }
    )
    assert evidence_of(record, _Proven()).status == "eligible"
    assert evidence_of(record).status == "ineligible"
