"""Operational evidence bundle. A filled form is not a passed check.

Collect a bundle on the execution host, not on the control plane. Record the
host identity, operating system and architecture, hypervisor, immutable image
digest, per-run microVM id, and the command that was measured. Hash those
bytes and store the hash with the audit event ids. Protect the bundle with
the host's integrity controls. A person reviews it. The bundle is stale when
the host identity, image digest, or measured configuration changes. This
module does not perform that review and does not read the environment.
`review` returns `unavailable()` for every bundle, including one marked
`production_approved`.
"""

from dataclasses import dataclass

from flakepilot.execution.domain.capability import LEVELS
from flakepilot.execution.domain.evidence import IsolationChecks, unavailable

__all__ = ["EvidenceBundle", "review"]

_FACTS = (
    "network_measured",
    "resources_measured",
    "credentials_checked",
    "cleanup_measured",
)


@dataclass(frozen=True)
class EvidenceBundle:
    """Measurements a reviewer can read. Not a signature and not a check."""

    level: str
    host_id: str
    os_arch: str
    hypervisor: str
    image_digest: str
    microvm_id: str
    network_measured: bool
    resources_measured: bool
    credentials_checked: bool
    cleanup_measured: bool
    artifact_note: str
    audit_ids: tuple[str, ...]
    command: str
    result: str
    environment: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.level not in LEVELS or not isinstance(self.audit_ids, tuple):
            raise ValueError("rejected")
        if not _sha(self.evidence_sha256):
            raise ValueError("rejected")
        for name in _FACTS:
            if type(getattr(self, name)) is not bool:
                raise ValueError("rejected")
        texts = (
            self.host_id,
            self.os_arch,
            self.hypervisor,
            self.image_digest,
            self.microvm_id,
            self.artifact_note,
            self.command,
            self.result,
            self.environment,
        )
        if any(
            not isinstance(item, str) or not item or len(item) > 200 for item in texts
        ):
            raise ValueError("rejected")


def review(bundle: EvidenceBundle) -> IsolationChecks:
    """Refuse every bundle. Independent review is not this function."""
    if not isinstance(bundle, EvidenceBundle):
        return unavailable()
    return unavailable()


def _sha(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
