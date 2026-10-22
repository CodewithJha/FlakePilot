"""The ledger file is mode 0600 and is not a symlink. No container is started."""

import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from flakepilot.execution.application.host import (
    HostGate,
    HostRejected,
    LedgerError,
    seal,
)
from flakepilot.execution.application.plane import ReapResult
from flakepilot.execution.infrastructure.ledger import FileLedger
from flakepilot.execution.infrastructure.reaper import LabelledContainerReaper

EXECUTION = "22222222-2222-2222-2222-222222222222"
ONE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
TWO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
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
    lock = path.with_name(".ledger.lock")
    assert (lock.stat().st_mode & 0o777) == 0o600


def test_a_missing_ledger_is_not_an_empty_authority(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    with pytest.raises(LedgerError):
        FileLedger(path).lookup(EXECUTION)
    assert not path.exists()
    result = _reap(path, f"{LIVE}\t{EXECUTION}\t99999")
    assert result.reclaimed == 0
    assert result.reason == "runtime_reaper_unavailable"
    assert not path.exists()


def test_an_empty_or_malformed_ledger_removes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    path.write_bytes(b"")
    path.chmod(0o600)
    result = _reap(path, f"{LIVE}\t{EXECUTION}\t1")
    assert result.reclaimed == 0
    assert result.reason == "runtime_reaper_unavailable"
    path.write_text("not-a-row\n", encoding="ascii")
    path.chmod(0o600)
    assert _reap(path, f"{LIVE}\t{EXECUTION}\t1").reclaimed == 0
    line = f"{EXECUTION} 10 0\n"
    path.write_text(line + line, encoding="ascii")
    path.chmod(0o600)
    assert _reap(path, f"{LIVE}\t{EXECUTION}\t1").reclaimed == 0
    path.write_bytes(b"a" * 8193)
    path.chmod(0o600)
    assert _reap(path, f"{LIVE}\t{EXECUTION}\t1").reclaimed == 0


def test_a_second_expiry_is_not_stored(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 40)
    before = path.read_bytes()
    with pytest.raises(HostRejected):
        FileLedger(path).register(EXECUTION, 90)
    assert path.read_bytes() == before
    assert FileLedger(path).lookup(EXECUTION) == (True, False, 40)


def test_cancel_blocks_a_later_register(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).cancel(EXECUTION)
    with pytest.raises(HostRejected):
        FileLedger(path).register(EXECUTION, 9)
    assert FileLedger(path).lookup(EXECUTION) == (True, True, 0)


def test_host_gate_register_is_what_the_file_keeps(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    key = b"k" * 32
    message = f"register {EXECUTION} 40".encode()
    HostGate(FileLedger(path), key).accept(message, seal(message, key))
    assert FileLedger(path).lookup(EXECUTION) == (True, False, 40)


def test_a_short_write_still_stores_the_whole_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write

    def short(descriptor: int, data: bytes) -> int:
        if not data:
            return 0
        return real(descriptor, data[:1])

    monkeypatch.setattr(os, "write", short)
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 40)
    assert FileLedger(path).lookup(EXECUTION) == (True, False, 40)


def test_a_store_fsyncs_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = os.fsync

    def wrapped(descriptor: int) -> None:
        calls.append(descriptor)
        real(descriptor)

    monkeypatch.setattr(os, "fsync", wrapped)
    FileLedger(tmp_path / "ledger").register(EXECUTION, 40)
    assert len(calls) >= 2


def test_a_symlink_lock_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "other"
    target.write_text("leave-this", encoding="ascii")
    path = tmp_path / "ledger"
    path.with_name(".ledger.lock").symlink_to(target)
    with pytest.raises(LedgerError):
        FileLedger(path).register(EXECUTION, 5)
    assert target.read_text(encoding="ascii") == "leave-this"
    assert not path.exists()


def test_a_group_writable_lock_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    lock = path.with_name(".ledger.lock")
    lock.write_bytes(b"")
    lock.chmod(0o666)
    with pytest.raises(LedgerError):
        FileLedger(path).register(EXECUTION, 5)
    assert not path.exists()
    assert (lock.stat().st_mode & 0o777) == 0o666


def test_a_contended_lock_leaves_the_ledger_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    FileLedger(path).register(EXECUTION, 40)
    before = path.read_bytes()
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_hold_lock, args=(str(path), str(ready), str(release))
    )
    process.start()
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            if time.monotonic() > deadline:
                raise AssertionError("lock holder did not start")
            time.sleep(0.05)
        with pytest.raises(LedgerError):
            FileLedger(path).cancel(EXECUTION)
    finally:
        release.write_text("go", encoding="ascii")
        process.join(10)
    assert process.exitcode == 0
    assert path.read_bytes() == before
    assert FileLedger(path).lookup(EXECUTION) == (True, False, 40)


def test_concurrent_registers_keep_both_ids(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        for _ in range(12):
            path.unlink(missing_ok=True)
            first = pool.submit(_register, str(path), ONE, 100)
            second = pool.submit(_register, str(path), TWO, 200)
            assert first.result(timeout=20) == "stored"
            assert second.result(timeout=20) == "stored"
            assert FileLedger(path).lookup(ONE) == (True, False, 100)
            assert FileLedger(path).lookup(TWO) == (True, False, 200)


def test_concurrent_register_and_cancel_stays_cancelled(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        for _ in range(12):
            path.unlink(missing_ok=True)
            registered = pool.submit(_register, str(path), ONE, 500)
            cancelled = pool.submit(_cancel, str(path), ONE)
            assert registered.result(timeout=20) in {"stored", "rejected"}
            assert cancelled.result(timeout=20) == "stored"
            found, cancelled_row, expires = FileLedger(path).lookup(ONE)
            assert found is True
            assert cancelled_row is True
            assert expires in {0, 500}


class _Calls:
    def __init__(self, listing: str) -> None:
        self.listing = listing
        self.commands: list[str] = []

    def __call__(self, argv: list[str]) -> str:
        self.commands.append(argv[1])
        if argv[1] == "ps":
            return self.listing
        return ""


def _reap(path: Path, listing: str) -> ReapResult:
    return LabelledContainerReaper(
        _Calls(listing), now=lambda: 200, authority=FileLedger(path)
    ).reclaim()


def _register(path: str, execution_id: str, expires: int) -> str:
    try:
        FileLedger(Path(path)).register(execution_id, expires)
    except HostRejected:
        return "rejected"
    return "stored"


def _cancel(path: str, execution_id: str) -> str:
    FileLedger(Path(path)).cancel(execution_id)
    return "stored"


def _hold_lock(path: str, ready: str, release: str) -> None:
    import fcntl

    lock = Path(path).with_name("." + Path(path).name + ".lock")
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock, flags)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    Path(ready).write_text("ready", encoding="ascii")
    deadline = time.monotonic() + 30
    while not Path(release).exists():
        if time.monotonic() > deadline:
            break
        time.sleep(0.05)
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)
