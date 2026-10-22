"""Restart and failure recovery. The runtime CLI is fake. No container is started."""

import os
import threading
from pathlib import Path

import pytest

from flakepilot.execution.application.host import (
    ExecutionAuthority,
    HostRejected,
    LedgerError,
    MemoryLedger,
)
from flakepilot.execution.application.plane import (
    RefusedHostHandoff,
    control_plane_reaper,
)
from flakepilot.execution.domain.plane import container_action
from flakepilot.execution.infrastructure.ledger import FileLedger
from flakepilot.execution.infrastructure.reaper import LabelledContainerReaper

EXECUTION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
CONTAINER = "abc123abc123"
FOREIGN = "def456def456"


class _Cli:
    def __init__(self, rows: list[str]) -> None:
        self.rows = rows
        self.removals: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, argv: list[str]) -> str:
        with self._lock:
            if argv[1] == "ps":
                return "\n".join(self.rows)
            container_id = argv[-1]
            self.removals.append(container_id)
            self.rows = [
                row for row in self.rows if not row.startswith(container_id + "\t")
            ]
            return ""


def _reap(cli: _Cli, ledger: FileLedger, *, now: int) -> tuple[int, str]:
    result = LabelledContainerReaper(cli, now=lambda: now, authority=ledger).reclaim()
    return result.reclaimed, result.reason


def test_a_refused_handoff_stores_nothing(tmp_path: Path) -> None:
    handoff = RefusedHostHandoff()
    assert handoff.register(EXECUTION, 500) == "host_authority_unavailable"
    assert handoff.cancel(EXECUTION) == "host_authority_unavailable"
    assert handoff.complete(EXECUTION) == "host_authority_unavailable"
    assert list(tmp_path.iterdir()) == []
    try:
        handoff.register("not-an-id", 500)
    except HostRejected as exc:
        assert str(exc) == "rejected"
    else:
        raise AssertionError("invalid id")


def test_worker_restart_leaves_a_persisted_registration(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 500)
    report = control_plane_reaper().reclaim()
    assert (report.reclaimed, report.reason) == (0, "runtime_reaper_unavailable")
    again = FileLedger(path)
    assert again.lookup(EXECUTION) == (True, False, 500)
    assert path.read_bytes()


def test_a_restarted_reaper_reads_the_persisted_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 100)
    FileLedger(path).register(OTHER, 500)
    del path
    ledger_path = tmp_path / "ledger"
    cli = _Cli([f"{CONTAINER}\t{EXECUTION}\t999", f"{FOREIGN}\t{OTHER}\t1"])
    reclaimed, reason = _reap(cli, FileLedger(ledger_path), now=200)
    assert (reclaimed, reason) == (1, "expired_containers_removed")
    assert cli.removals == [CONTAINER]
    assert FileLedger(ledger_path).lookup(OTHER) == (True, False, 500)


def test_expiry_and_cancel_both_remove_and_either_order_sticks(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    ledger = FileLedger(path)
    ledger.register(EXECUTION, 100)
    ledger.cancel(EXECUTION)
    registered, cancelled, expires = FileLedger(path).lookup(EXECUTION)
    assert (
        container_action(
            registered=registered, cancelled=cancelled, expires=expires, now=50
        )
        == "remove"
    )
    assert (
        container_action(registered=True, cancelled=False, expires=100, now=100)
        == "remove"
    )
    try:
        FileLedger(path).register(EXECUTION, 100)
    except HostRejected:
        pass
    else:
        raise AssertionError("cancel tombstone")
    assert FileLedger(path).lookup(EXECUTION)[1] is True


def test_a_dropped_removal_count_does_not_restore_the_container() -> None:
    cli = _Cli([f"{CONTAINER}\t{EXECUTION}\t100"])
    ledger_rows = MemoryLedger()
    ledger_rows.register(EXECUTION, 100)
    first = LabelledContainerReaper(
        cli, now=lambda: 200, authority=ledger_rows
    ).reclaim()
    del first
    second = LabelledContainerReaper(
        cli, now=lambda: 200, authority=ledger_rows
    ).reclaim()
    assert second.reclaimed == 0
    assert cli.removals == [CONTAINER]


def test_a_wrong_owner_ledger_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 100)
    real_uid = os.getuid()
    monkeypatch.setattr(
        "flakepilot.execution.infrastructure.ledger.os.getuid",
        lambda: real_uid + 1,
    )
    cli = _Cli([f"{CONTAINER}\t{EXECUTION}\t100"])
    reclaimed, reason = _reap(cli, FileLedger(path), now=200)
    assert (reclaimed, reason) == (0, "runtime_reaper_unavailable")
    assert cli.removals == []


def test_an_unavailable_authority_removes_nothing() -> None:
    class _Down(ExecutionAuthority):
        def lookup(self, execution_id: str) -> tuple[bool, bool, int]:
            del execution_id
            raise LedgerError

    cli = _Cli([f"{CONTAINER}\t{EXECUTION}\t100"])
    result = LabelledContainerReaper(cli, now=lambda: 200, authority=_Down()).reclaim()
    assert (result.reclaimed, result.reason) == (0, "runtime_reaper_unavailable")
    assert cli.removals == []


def test_an_unknown_container_id_is_not_a_removal_target() -> None:
    cli = _Cli([f"zz\t{EXECUTION}\t100", f"{CONTAINER}\tnot-a-uuid\t100"])
    ledger = MemoryLedger()
    ledger.register(EXECUTION, 100)
    result = LabelledContainerReaper(cli, now=lambda: 200, authority=ledger).reclaim()
    assert result.reclaimed == 0
    assert cli.removals == []


def test_two_sweeps_remove_one_expired_container_once() -> None:
    cli = _Cli([f"{CONTAINER}\t{EXECUTION}\t100", f"{FOREIGN}\t{OTHER}\t500"])
    ledger = MemoryLedger()
    ledger.register(EXECUTION, 100)
    ledger.register(OTHER, 500)

    def sweep() -> None:
        LabelledContainerReaper(cli, now=lambda: 200, authority=ledger).reclaim()

    first = threading.Thread(target=sweep)
    second = threading.Thread(target=sweep)
    first.start()
    second.start()
    first.join()
    second.join()
    assert cli.removals
    assert set(cli.removals) == {CONTAINER}
    third = LabelledContainerReaper(cli, now=lambda: 200, authority=ledger).reclaim()
    assert third.reclaimed == 0
    assert cli.rows == [f"{FOREIGN}\t{OTHER}\t500"]


def test_a_rejected_register_does_not_create_a_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    try:
        FileLedger(path).register("nope", 10)
    except HostRejected:
        pass
    else:
        raise AssertionError("partial row")
    assert not path.exists()
