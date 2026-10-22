"""The runner harness is a measurement script, not an isolation attestation.

Reading these files does not pass a check. Production isolation stays false
until a dedicated host is measured, and this test does not flip that.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "isolation-validation.yml"
RUNNER = ROOT / "ops" / "isolation" / "run.sh"
PROBE = ROOT / "ops" / "isolation" / "probe.py"

PIN = "sha256:e8a38829209aea82774882e5157e1f7c3e8c8393ff53a697b3580896e57f345c"
MAC_PIN = "sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
MAC_SH = ROOT / "ops" / "isolation" / "local_mac_probe.sh"
MAC_DRIVER = ROOT / "ops" / "isolation" / "local_mac_driver.py"


def test_harness_pins_the_image_and_does_not_pass_a_secret() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    script = RUNNER.read_text(encoding="utf-8")
    probe = PROBE.read_text(encoding="utf-8")
    combined = workflow + script
    assert "pull_request_target" not in workflow
    assert "workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "persist-credentials: false" in workflow
    assert "contents: read" in workflow
    assert 'GITHUB_TOKEN: ""' in workflow
    assert PIN in script
    assert "--network=none" in script
    assert "--cap-drop=ALL" in script
    assert "--security-opt=no-new-privileges" in script
    assert "--user=65534:65534" in script
    assert "execution_enabled=false" in script
    assert "isolation_checks=false" in script
    assert "NOT_EXECUTED" in script
    for banned in (
        "--privileged",
        "docker.sock",
        "--network=host",
        "--cap-add",
        "-e GITHUB_TOKEN",
        "--env GITHUB_TOKEN",
        "FLAKEPILOT_DATABASE_URL",
        "FLAKEPILOT_SECRET_KEY",
        "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY",
        "network_denied=True",
        "IsolationChecks(",
    ):
        assert banned not in combined
    assert "docker.sock" in probe
    assert "169.254.169.254" in probe


def test_the_mac_probe_is_scoped_and_does_not_pass_isolation() -> None:
    """Reading the probe does not run it and does not mark a check passed."""
    script = MAC_SH.read_text(encoding="utf-8")
    driver = MAC_DRIVER.read_text(encoding="utf-8")
    assert script.startswith("#!/bin/bash\n")
    assert "exec /bin/bash" in script
    assert "trap cleanup EXIT" in script
    assert "flakepilot.macrun=" in script
    assert MAC_PIN in driver
    assert "size=64m" in driver
    assert "FileLedger" in driver
    assert "HostGate" in driver
    assert "LabelledContainerReaper" in driver
    assert "execution_enabled=false" in driver
    assert "isolation_checks=false" in driver
    assert "NOT EXECUTED" in driver
    for banned in (
        "--privileged",
        "docker.sock",
        "--network=host",
        "--cap-add",
        "IsolationChecks(",
        "network_denied=True",
        "execution_enabled=true",
        "FLAKEPILOT_DATABASE_URL",
        "FLAKEPILOT_SECRET_KEY",
        "FLAKEPILOT_GITHUB_APP_PRIVATE_KEY",
    ):
        assert banned not in script
        assert banned not in driver
