"""When a stored reproduction may be trusted evidence.

Eligible means a completed `success` or `test_failure` whose org, repo, and SHA
are well formed, whose stored evidence is complete, and whose isolation checks
all passed. One such result is not a flakiness claim. This module does not run
a sandbox and does not talk to a code host.
"""

import uuid
from dataclasses import dataclass

from flakepilot.execution.domain.reproduction import sha_ok

_ELIGIBLE = frozenset({"success", "test_failure"})


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class IsolationChecks:
    """Seven host facts. This deployment can only report that they are unknown."""

    network_denied: bool
    metadata_denied: bool
    host_mounts_denied: bool
    credentials_absent: bool
    unprivileged: bool
    limits_enforced: bool
    cleanup_verified: bool

    def passed(self) -> bool:
        return (
            self.network_denied
            and self.metadata_denied
            and self.host_mounts_denied
            and self.credentials_absent
            and self.unprivileged
            and self.limits_enforced
            and self.cleanup_verified
        )


def unavailable() -> IsolationChecks:
    """Every check false. The only attestation production code may construct."""
    return IsolationChecks(
        network_denied=False,
        metadata_denied=False,
        host_mounts_denied=False,
        credentials_absent=False,
        unprivileged=False,
        limits_enforced=False,
        cleanup_verified=False,
    )


@dataclass(frozen=True)
class EvidenceVerdict:
    """`status` is `eligible` or `ineligible`. `reason` keeps the outcome class."""

    status: str
    reason: str


def accept(
    *,
    outcome: str | None,
    state: str,
    reason: str | None,
    sha: str,
    org_id: str,
    repo_id: str,
    evidence_partial: bool,
    superseded: bool,
    checks: IsolationChecks,
) -> EvidenceVerdict:
    """Decide eligibility. Missing proof stays ineligible. Never returns `flaky`."""
    if not (_uuid(org_id) and _uuid(repo_id)):
        return EvidenceVerdict("ineligible", "tenant")
    if superseded:
        return EvidenceVerdict("ineligible", "superseded")
    if state == "cancelled" or outcome == "cancelled" or reason == "cancelled":
        return EvidenceVerdict("ineligible", "cancelled")
    if outcome == "timeout" or reason == "timeout":
        return EvidenceVerdict("ineligible", "timeout")
    if reason == "execution_closed":
        return EvidenceVerdict("ineligible", "execution_closed")
    if reason == "source_unverified" or not sha_ok(sha):
        return EvidenceVerdict("ineligible", "source_unverified")
    if outcome == "infra_failure":
        return EvidenceVerdict("ineligible", "infra_failure")
    if outcome == "invalid_result":
        return EvidenceVerdict("ineligible", "invalid_result")
    if outcome == "inconclusive" or state != "completed":
        return EvidenceVerdict("ineligible", "inconclusive")
    if evidence_partial:
        return EvidenceVerdict("ineligible", "incomplete")
    if outcome not in _ELIGIBLE:
        return EvidenceVerdict("ineligible", "invalid_result")
    if not checks.passed():
        return EvidenceVerdict("ineligible", "isolation_unverified")
    return EvidenceVerdict("eligible", outcome)
