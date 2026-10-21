"""The ledger file is mode 0600 and is not a symlink. No container is started."""

import os
from pathlib import Path

import pytest

from flakepilot.execution.application.host import LedgerError
from flakepilot.execution.infrastructure.ledger import FileLedger
from flakepilot.execution.infrastructure.reaper import LabelledContainerReaper

EXECUTION = "22222222-2222-2222-2222-222222222222"
LIVE = "def456def456"


def test_register_and_cancel_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 500)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert FileLedger(path).lookup(EXECUTION) == (True, False, 500)
    FileLedger(path).cancel(EXECUTION)
    assert FileLedger(path).lookup(EXECUTION) == (True, True, 500)


def test_a_world_writable_ledger_removes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 100)
    path.chmod(0o666)

    class Cli:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, argv: list[str]) -> str:
            self.calls.append(list(argv))
            if argv[1] == "ps":
                return f"{LIVE}\t{EXECUTION}\t99999"
            return ""

    cli = Cli()
    result = LabelledContainerReaper(
        cli, now=lambda: 200, authority=FileLedger(path)
    ).reclaim()
    assert result.reclaimed == 0
    assert result.reason == "runtime_reaper_unavailable"
    assert [call[1] for call in cli.calls] == ["ps"]


def test_a_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("11111111-1111-1111-1111-111111111111 999 0\n", encoding="ascii")
    link = tmp_path / "ledger"
    link.symlink_to(target)
    with pytest.raises(LedgerError):
        FileLedger(link).lookup(EXECUTION)
    assert target.read_text(encoding="ascii").startswith("11111111")


def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(LedgerError):
        FileLedger(Path("ledger"))


def test_the_directory_is_not_left_group_writable(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    old = os.umask(0o022)
    try:
        FileLedger(path).register(EXECUTION, 40)
    finally:
        os.umask(old)
    assert (path.stat().st_mode & 0o777) == 0o600
