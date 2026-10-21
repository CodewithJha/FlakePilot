import os
import socket
import subprocess
import sys

import pytest
from support import SETTINGS

from flakepilot.interface.composition_root import Secret, Settings
from flakepilot.interface.composition_root.bootstrap import run_bootstrap_org


@pytest.mark.parametrize(
    ("argv", "field"),
    [
        (["--name", " Acme", "--admin-github-id", "42"], "--name"),
        (["--name", "", "--admin-github-id", "42"], "--name"),
        (["--name", "Acme", "--admin-github-id", "4x2"], "--admin-github-id"),
        (["--name", "Acme", "--admin-github-id", "0"], "--admin-github-id"),
        (
            ["--name", "Acme", "--admin-github-id", "42", "--admin-login", "a b"],
            "--admin-login",
        ),
    ],
)
def test_invalid_arguments_are_refused_before_any_database_access(
    argv: list[str], field: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_bootstrap_org(SETTINGS, argv) == 1  # SETTINGS points at db.invalid
    out, err = capsys.readouterr()
    assert out == "" and err.startswith(f"bootstrap-org: {field}")


def test_missing_arguments_print_usage() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_bootstrap_org(SETTINGS, ["--name", "Acme"])
    assert excinfo.value.code == 2


def test_unreachable_database_is_reported_by_class_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as probe:  # a port with nothing listening
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dsn = f"postgresql://flakepilot:boot-SENTINEL-pw@127.0.0.1:{port}/flakepilot"
    argv = ["--name", "Acme", "--admin-github-id", "42"]
    assert run_bootstrap_org(Settings(Secret(dsn)), argv) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "bootstrap-org failed: ConnectionRefusedError\n"


def test_server_entry_point_refuses_unknown_commands() -> None:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FLAKEPILOT_")}
    result = subprocess.run(
        [sys.executable, "-m", "flakepilot.interface.composition_root", "frobnicate"],
        env={**environ, "FLAKEPILOT_DATABASE_URL": "postgresql://u@db.invalid/x"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "Usage:" in result.stderr and "Traceback" not in result.stderr
