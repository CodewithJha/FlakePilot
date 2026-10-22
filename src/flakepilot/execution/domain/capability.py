"""Isolation evidence ladder. A lower observation never satisfies a higher level.

Levels, weakest first: simulation, shared_kernel_container,
dedicated_host_container, per_run_microvm, production_approved. Every level
requires the same controls. Evidence collected at one level does not count
at a stronger level. Docker on a shared kernel is `shared_kernel_container`.
It does not satisfy `per_run_microvm` or `production_approved`.

`attest` is a pure function for tests of this ladder. It is not a deployment
attestation. Production code constructs `unavailable()` and does not call
`attest`. A true record is not a passed check for this deployment.
"""

from dataclasses import dataclass

from flakepilot.execution.domain.evidence import IsolationChecks, unavailable

__all__ = [
    "CONTROLS",
    "LEVELS",
    "ControlEvidence",
    "attest",
    "evidence_satisfies",
]

LEVELS = (
    "simulation",
    "shared_kernel_container",
    "dedicated_host_container",
    "per_run_microvm",
    "production_approved",
)

CONTROLS = (
    "network_egress_denied",
    "metadata_endpoint_denied",
    "control_plane_endpoint_denied",
    "database_url_absent",
    "session_secret_absent",
    "github_app_key_absent",
    "host_ledger_key_absent",
    "cpu_limit",
    "memory_limit",
    "pid_limit",
    "disk_limit",
    "output_limit",
    "wall_clock_limit",
    "host_cleanup",
    "filesystem_scoped",
    "artifact_isolation",
    "image_provenance",
    "audit_recovery",
    "unprivileged",
    "host_mounts_denied",
)

_RANK = {level: index for index, level in enumerate(LEVELS)}

# One IsolationChecks field is true only when every listed control was observed.
_FIELDS: dict[str, tuple[str, ...]] = {
    "network_denied": ("network_egress_denied",),
    "metadata_denied": (
        "metadata_endpoint_denied",
        "control_plane_endpoint_denied",
    ),
    "host_mounts_denied": ("host_mounts_denied", "filesystem_scoped"),
    "credentials_absent": (
        "database_url_absent",
        "session_secret_absent",
        "github_app_key_absent",
        "host_ledger_key_absent",
    ),
    "unprivileged": ("unprivileged",),
    "limits_enforced": (
        "cpu_limit",
        "memory_limit",
        "pid_limit",
        "disk_limit",
        "output_limit",
        "wall_clock_limit",
    ),
    "cleanup_verified": (
        "host_cleanup",
        "artifact_isolation",
        "image_provenance",
        "audit_recovery",
    ),
}


@dataclass(frozen=True)
class ControlEvidence:
    """One observed control. `observed` is accepted only as a real bool."""

    level: str
    control: str
    observed: bool


def evidence_satisfies(required: str, observed: str) -> bool:
    """True when `observed` is the required level or a stronger one."""
    required_rank = _RANK.get(required)
    observed_rank = _RANK.get(observed)
    if required_rank is None or observed_rank is None:
        return False
    return observed_rank >= required_rank


def attest(records: tuple[ControlEvidence, ...], required: str) -> IsolationChecks:
    """Checks supported by `records` at `required` or above.

    A missing control stays false. A record from a weaker level is ignored.
    Garbage input returns every check false. This does not read a host.
    """
    if required not in _RANK or not isinstance(records, tuple):
        return unavailable()
    seen = {control: False for control in CONTROLS}
    for record in records:
        if not isinstance(record, ControlEvidence) or not isinstance(
            record.observed, bool
        ):
            return unavailable()
        if record.control not in seen or not record.observed:
            continue
        if evidence_satisfies(required, record.level):
            seen[record.control] = True
    flags = {
        field: all(seen[control] for control in controls)
        for field, controls in _FIELDS.items()
    }
    return IsolationChecks(
        network_denied=flags["network_denied"],
        metadata_denied=flags["metadata_denied"],
        host_mounts_denied=flags["host_mounts_denied"],
        credentials_absent=flags["credentials_absent"],
        unprivileged=flags["unprivileged"],
        limits_enforced=flags["limits_enforced"],
        cleanup_verified=flags["cleanup_verified"],
    )
