"""The opt-in commands (agent bindings, Atlas export, Temporal POC) are reachable
from the entry point, refuse to run unconfigured, and are never part of the API
process or the default worker."""

import subprocess
import sys
from pathlib import Path

import pytest

import flakepilot.interface.composition_root.__main__ as entry
from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.atlas_export import (
    run_export_reports_atlas,
)
from flakepilot.interface.composition_root.temporal_investigation import (
    run_temporal_cancel,
    run_temporal_investigation,
    run_temporal_worker,
)

DSN = "postgresql://usr_x1:pw_Q7z@host_k2:5432/db_m3"
ROOT = Path(__file__).resolve().parents[3]
SETTINGS = Settings(Secret(DSN))
ORG, RUN = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)


@pytest.mark.parametrize(
    "command",
    [
        "bind-agent",
        "unbind-agent",
        "export-reports-atlas",
        "temporal-worker",
        "temporal-run-investigation",
        "temporal-cancel-investigation",
    ],
)
def test_each_command_is_dispatched(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FLAKEPILOT_DATABASE_URL", DSN)
    monkeypatch.setattr(sys, "argv", ["flakepilot", command, "--help"])
    with pytest.raises(SystemExit) as excinfo:
        entry.main()
    assert excinfo.value.code == 0
    assert command in capsys.readouterr().out
    assert command in entry._USAGE


def test_unconfigured_commands_refuse_before_any_connection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_export_reports_atlas(SETTINGS, ["--org-id", ORG, "--repo-id", RUN]) == 1
    assert run_temporal_worker(SETTINGS, []) == 1
    assert (
        run_temporal_investigation(
            SETTINGS,
            ["--org-id", ORG, "--run-id", RUN, "--member-github-id", "4242"],
        )
        == 1
    )
    assert run_temporal_cancel(SETTINGS, ["--key", RUN]) == 1
    err = capsys.readouterr().err
    assert "needs FLAKEPILOT_ATLAS_URI" in err
    assert err.count("needs FLAKEPILOT_TEMPORAL_ADDRESS") == 3


def test_malformed_temporal_requests_are_refused_without_contact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["--org-id", "nope", "--run-id", RUN, "--member-github-id", "4242"]
    assert run_temporal_investigation(SETTINGS, args) == 1
    assert run_temporal_cancel(SETTINGS, ["--key", "nope"]) == 1
    assert "must be" in capsys.readouterr().err


def test_the_api_process_and_default_worker_never_load_the_opt_in_sdks() -> None:
    sources = "\n".join(
        (ROOT / "src/flakepilot/interface/composition_root" / name).read_text()
        for name in ("__init__.py", "worker.py")
    )
    for banned in ("temporal", "pymongo", "AtlasReportSink", "atlas_export"):
        assert banned not in sources
    probe = (
        "import sys; import flakepilot.interface.composition_root as c; "
        "import flakepilot.interface.composition_root.worker; "
        "print(any(m.split('.')[0] in ('temporalio', 'pymongo') for m in sys.modules))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert loaded.stdout.strip() == "False"
