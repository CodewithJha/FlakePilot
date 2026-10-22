"""Host recovery invariants. In-process only. No container and no socket.

A file ledger here is a simulated restart of the host object, not a reboot.
"""

import threading
from pathlib import Path

import pytest

from flakepilot.execution.application.host import (
    HostRejected,
    LedgerError,
    MemoryLedger,
    seal,
)
from flakepilot.execution.application.reference_host import ReferenceHost
from flakepilot.execution.infrastructure.ledger import FileLedger

EXEC = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOW = 1_700_000_000
CID = "ab" * 6
CURRENT = b"c" * 32
PREVIOUS = b"p" * 32


def _register(expires: int = NOW + 30) -> bytes:
    return f"register {EXEC} {expires}".encode()


def test_a_previous_key_still_verifies_until_it_is_removed() -> None:
    ledger = MemoryLedger()
    host = ReferenceHost(
        ledger,
        CURRENT,
        now=NOW,
        runtime_available=True,
        previous_keys=(PREVIOUS,),
    )
    message = _register()
    host.accept("register", message, seal(message, PREVIOUS))
    host.accept("register", message, seal(message, CURRENT))
    assert ledger.phase(EXEC) == "open"
    empty = MemoryLedger()
    revoked = ReferenceHost(empty, CURRENT, now=NOW, runtime_available=True)
    with pytest.raises(HostRejected):
        revoked.accept("register", message, seal(message, PREVIOUS))
    assert empty.rows() == ()


def test_a_revoked_mac_does_not_change_an_open_row() -> None:
    ledger = MemoryLedger()
    host = ReferenceHost(
        ledger, CURRENT, now=NOW, runtime_available=True, previous_keys=(PREVIOUS,)
    )
    message = _register()
    host.accept("register", message, seal(message, CURRENT))
    narrowed = ReferenceHost(ledger, CURRENT, now=NOW, runtime_available=True)
    cancel = f"cancel {EXEC}".encode()
    with pytest.raises(HostRejected):
        narrowed.accept("cancel", cancel, seal(cancel, PREVIOUS))
    assert ledger.phase(EXEC) == "open"


def test_expiry_selects_cleanup_without_rewriting_the_phase(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    host = ReferenceHost(FileLedger(path), CURRENT, now=NOW, runtime_available=True)
    message = _register(NOW + 5)
    host.accept("register", message, seal(message, CURRENT))
    later = ReferenceHost(
        FileLedger(path), CURRENT, now=NOW + 5, runtime_available=True
    )
    assert later.cleanup(((CID, EXEC),)) == ("ok", (CID,))
    assert FileLedger(path).phase(EXEC) == "open"
    assert path.read_bytes().endswith(b" 0\n")


def test_a_missing_or_corrupt_ledger_is_not_an_empty_success(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    host = ReferenceHost(FileLedger(missing), CURRENT, now=NOW, runtime_available=True)
    assert host.cleanup(((CID, EXEC),)) == ("authority_unavailable", ())
    assert not missing.exists()
    corrupt = tmp_path / "corrupt"
    corrupt.write_text("not a ledger\n", encoding="ascii")
    corrupt.chmod(0o600)
    broken = ReferenceHost(
        FileLedger(corrupt), CURRENT, now=NOW, runtime_available=True
    )
    message = _register()
    with pytest.raises(LedgerError):
        broken.accept("register", message, seal(message, CURRENT))
    assert corrupt.read_text(encoding="ascii") == "not a ledger\n"


def test_concurrent_cancel_and_complete_keep_one_mark(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    for _ in range(8):
        path.unlink(missing_ok=True)
        opener = ReferenceHost(
            FileLedger(path), CURRENT, now=NOW, runtime_available=True
        )
        message = _register()
        opener.accept("register", message, seal(message, CURRENT))
        barrier = threading.Barrier(2)
        results: list[str] = []

        def run(
            operation: str,
            body: bytes,
            gate: threading.Barrier = barrier,
            found: list[str] = results,
        ) -> None:
            gate.wait()
            local = ReferenceHost(
                FileLedger(path), CURRENT, now=NOW, runtime_available=True
            )
            try:
                local.accept(operation, body, seal(body, CURRENT))
            except HostRejected:
                found.append(operation + "-rejected")
                return
            found.append(operation)

        cancel = f"cancel {EXEC}".encode()
        complete = f"complete {EXEC}".encode()
        threads = (
            threading.Thread(target=run, args=("cancel", cancel)),
            threading.Thread(target=run, args=("complete", complete)),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        phase = FileLedger(path).phase(EXEC)
        assert phase in {"cancelled", "completed"}
        assert tuple(sorted(results)) in {
            ("cancel", "complete-rejected"),
            ("cancel-rejected", "complete"),
        }


def test_concurrent_cleanup_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger"
    host = ReferenceHost(FileLedger(path), CURRENT, now=NOW, runtime_available=True)
    cancel = f"cancel {EXEC}".encode()
    host.accept("cancel", cancel, seal(cancel, CURRENT))
    before = path.read_bytes()
    found: list[tuple[str, tuple[str, ...]]] = []
    barrier = threading.Barrier(2)

    def sweep() -> None:
        barrier.wait()
        local = ReferenceHost(
            FileLedger(path), CURRENT, now=NOW, runtime_available=True
        )
        found.append(local.cleanup(((CID, EXEC),)))

    threads = (threading.Thread(target=sweep), threading.Thread(target=sweep))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert found == [("ok", (CID,)), ("ok", (CID,))]
    assert path.read_bytes() == before
    assert FileLedger(path).phase(EXEC) == "cancelled"
