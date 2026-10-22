"""Evidence bundles do not pass isolation checks. No host was measured."""

from pathlib import Path
from typing import cast

import pytest

from flakepilot.execution.domain.bundle import EvidenceBundle, review
from flakepilot.execution.domain.evidence import unavailable

_DIGEST = "ab" * 32
_ROOT = Path(__file__).resolve().parents[3]


def _bundle(level: str) -> EvidenceBundle:
    return EvidenceBundle(
        level=level,
        host_id="host-a",
        os_arch="linux/arm64",
        hypervisor="none",
        image_digest="sha256:" + _DIGEST,
        microvm_id="not-a-microvm",
        network_measured=True,
        resources_measured=True,
        credentials_checked=True,
        cleanup_measured=True,
        artifact_note="plaintext",
        audit_ids=("audit-1",),
        command="review",
        result="recorded",
        environment="unreviewed",
        evidence_sha256=_DIGEST,
    )


def test_a_complete_production_bundle_does_not_pass() -> None:
    checks = review(_bundle("production_approved"))
    assert checks == unavailable()
    assert not checks.passed()
    assert review(_bundle("simulation")) == unavailable()
    assert review(_bundle("shared_kernel_container")) == unavailable()


def test_the_composition_root_does_not_review_a_bundle() -> None:
    interface = _ROOT / "src/flakepilot/interface"
    for path in interface.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "flakepilot.execution.domain.bundle" not in text
        assert "EvidenceBundle" not in text


def test_a_non_boolean_fact_is_rejected() -> None:
    with pytest.raises(ValueError, match="rejected"):
        EvidenceBundle(
            level="simulation",
            host_id="host-a",
            os_arch="linux/arm64",
            hypervisor="none",
            image_digest="sha256:" + _DIGEST,
            microvm_id="not-a-microvm",
            network_measured=cast(bool, 1),
            resources_measured=True,
            credentials_checked=True,
            cleanup_measured=True,
            artifact_note="plaintext",
            audit_ids=(),
            command="review",
            result="recorded",
            environment="unreviewed",
            evidence_sha256=_DIGEST,
        )
