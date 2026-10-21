"""The control-plane worker does not select a container or reap a runtime."""

import os
import time
from pathlib import Path

from flakepilot.integrations.infrastructure.sandbox import ClosedSandbox
from flakepilot.interface.composition_root.reproduction import _sandbox
from flakepilot.interface.composition_root.settings import Secret, Settings
from flakepilot.interface.composition_root.worker import startup_cleanup

IMAGE = "ghcr.io/example/reproduce@sha256:" + ("cd" * 32)


def test_evaluation_settings_still_use_the_closed_sandbox() -> None:
    settings = Settings(
        Secret("postgresql://localhost/flakepilot"),
        execution_mode="evaluation",
        execution_image=IMAGE,
    )
    assert isinstance(_sandbox(settings), ClosedSandbox)


def test_startup_cleanup_leaves_runtime_markers_and_reports_the_gap(
    tmp_path: Path,
) -> None:
    moment = time.time()
    expired = tmp_path / "fp-expired"
    expired.mkdir()
    os.utime(expired, (moment - 4000, moment - 4000))
    fresh = tmp_path / "fp-fresh"
    fresh.mkdir()
    marker = tmp_path / "runtime-id"
    marker.write_text("left", encoding="utf-8")
    removed, reason = startup_cleanup(tmp_path, now=moment)
    assert removed == 1
    assert not expired.exists()
    assert fresh.is_dir()
    assert marker.read_text(encoding="utf-8") == "left"
    assert reason == "runtime_reaper_unavailable"
