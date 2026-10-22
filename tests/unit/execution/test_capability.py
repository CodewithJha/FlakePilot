"""Isolation evidence ladder. No host was observed and no check is production evidence."""

from pathlib import Path
from typing import cast

from flakepilot.execution.domain.capability import (
    CONTROLS,
    LEVELS,
    ControlEvidence,
    attest,
    evidence_satisfies,
)
from flakepilot.execution.domain.evidence import unavailable

_ROOT = Path(__file__).resolve().parents[3]


def _records(level: str, *, observed: bool = True) -> tuple[ControlEvidence, ...]:
    return tuple(ControlEvidence(level, control, observed) for control in CONTROLS)


def test_levels_are_ordered_and_lower_evidence_never_satisfies_a_higher_one() -> None:
    assert LEVELS == (
        "simulation",
        "shared_kernel_container",
        "dedicated_host_container",
        "per_run_microvm",
        "production_approved",
    )
    assert evidence_satisfies("simulation", "simulation")
    assert evidence_satisfies("dedicated_host_container", "per_run_microvm")
    assert not evidence_satisfies("shared_kernel_container", "simulation")
    assert not evidence_satisfies("dedicated_host_container", "shared_kernel_container")
    assert not evidence_satisfies("per_run_microvm", "shared_kernel_container")
    assert not evidence_satisfies("per_run_microvm", "dedicated_host_container")
    assert not evidence_satisfies("production_approved", "per_run_microvm")
    assert not evidence_satisfies("production_approved", "simulation")
    assert not evidence_satisfies("production_approved", "not-a-level")
    assert not evidence_satisfies("not-a-level", "production_approved")


def test_shared_kernel_observations_do_not_pass_a_higher_requirement() -> None:
    records = _records("shared_kernel_container")
    for required in (
        "dedicated_host_container",
        "per_run_microvm",
        "production_approved",
    ):
        checks = attest(records, required)
        assert checks == unavailable()
        assert not checks.passed()


def test_one_missing_control_keeps_the_mapped_check_false() -> None:
    records = tuple(
        ControlEvidence(
            "production_approved",
            control,
            control != "github_app_key_absent",
        )
        for control in CONTROLS
    )
    checks = attest(records, "production_approved")
    assert not checks.credentials_absent
    assert checks.network_denied
    assert not checks.passed()


def test_a_false_or_non_boolean_observation_is_not_evidence() -> None:
    assert (
        attest(_records("production_approved", observed=False), "production_approved")
        == unavailable()
    )
    records = tuple(
        ControlEvidence("production_approved", control, cast(bool, 1))
        for control in CONTROLS
    )
    assert attest(records, "production_approved") == unavailable()


def test_unknown_controls_and_a_partial_set_do_not_pass() -> None:
    named = tuple(
        ControlEvidence("production_approved", name, True)
        for name in (
            "network_denied",
            "metadata_denied",
            "credentials_absent",
            "limits_enforced",
            "cleanup_verified",
        )
    )
    assert attest(named, "production_approved") == unavailable()
    almost = _records("simulation")[:-1]
    assert not attest(almost, "simulation").passed()


def test_the_same_controls_are_required_at_every_level() -> None:
    assert "network_egress_denied" in CONTROLS
    assert "metadata_endpoint_denied" in CONTROLS
    assert "control_plane_endpoint_denied" in CONTROLS
    assert "database_url_absent" in CONTROLS
    assert "session_secret_absent" in CONTROLS
    assert "github_app_key_absent" in CONTROLS
    assert "host_ledger_key_absent" in CONTROLS
    assert "cpu_limit" in CONTROLS
    assert "memory_limit" in CONTROLS
    assert "pid_limit" in CONTROLS
    assert "disk_limit" in CONTROLS
    assert "output_limit" in CONTROLS
    assert "wall_clock_limit" in CONTROLS
    assert "host_cleanup" in CONTROLS
    assert "filesystem_scoped" in CONTROLS
    assert "artifact_isolation" in CONTROLS
    assert "image_provenance" in CONTROLS
    assert "audit_recovery" in CONTROLS
    assert "unprivileged" in CONTROLS
    assert "host_mounts_denied" in CONTROLS
    simulation = attest(_records("simulation"), "simulation")
    assert simulation.passed()
    assert attest(_records("simulation"), "shared_kernel_container") == unavailable()


def test_production_sources_do_not_import_the_ladder() -> None:
    banned = ("flakepilot.execution.domain.capability", "attest(")
    interface = _ROOT / "src/flakepilot/interface"
    for path in interface.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in text
    worker = (_ROOT / "src/flakepilot/interface/composition_root/worker.py").read_text(
        encoding="utf-8"
    )
    assert "IsolationChecks" not in worker
    assert not unavailable().passed()
