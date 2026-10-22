"""Simulated reference host. No socket, no container, and no ledger file.

`runtime_available=True` only lets this process update an in-memory ledger and
select ids. It does not start a runtime. Isolation stays `simulation`.
"""

import secrets
from pathlib import Path

import pytest

from flakepilot.execution.application.host import HostRejected, MemoryLedger, seal
from flakepilot.execution.application.reference_host import (
    OPERATIONS,
    PROTOCOL,
    HostHealth,
    ReferenceHost,
)

EXEC = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NOW = 1_700_000_000
CID = "ab" * 6
LIVE = "cd" * 6
KEY = secrets.token_bytes(32)
_ROOT = Path(__file__).resolve().parents[3]


def _host(
    *,
    runtime_available: bool = False,
    authority_available: bool = True,
) -> tuple[ReferenceHost, MemoryLedger]:
    ledger = MemoryLedger()
    host = ReferenceHost(
        ledger,
        KEY,
        now=NOW,
        runtime_available=runtime_available,
        authority_available=authority_available,
    )
    return host, ledger


def _register(expires: int = NOW + 30) -> bytes:
    return f"register {EXEC} {expires}".encode()


def test_the_allowlist_and_protocol_are_version_one() -> None:
    assert PROTOCOL == 1
    assert OPERATIONS == frozenset(
        {"register", "cancel", "complete", "health", "cleanup"}
    )
    text = (_ROOT / "src/flakepilot/execution/application/reference_host.py").read_text(
        encoding="utf-8"
    )
    for name in ("subprocess", "docker", "socket", "os.environ", "FileLedger"):
        assert name not in text
    worker = (_ROOT / "src/flakepilot/interface/composition_root/worker.py").read_text(
        encoding="utf-8"
    )
    assert "ReferenceHost" not in worker


def test_register_requires_a_valid_mac_and_is_idempotent() -> None:
    host, ledger = _host(runtime_available=True)
    message = _register()
    host.accept("register", message, seal(message, KEY))
    host.accept("register", message, seal(message, KEY))
    assert ledger.lookup(EXEC) == (True, False, NOW + 30)
    with pytest.raises(HostRejected):
        host.accept("register", message, "ab" * 32)
    assert ledger.lookup(EXEC) == (True, False, NOW + 30)


def test_expired_malformed_and_unauthorized_requests_write_nothing() -> None:
    host, ledger = _host(runtime_available=True)
    expired = _register(NOW)
    with pytest.raises(HostRejected):
        host.accept("expired", expired, seal(expired, KEY))
    with pytest.raises(HostRejected):
        host.accept("register", expired, seal(expired, KEY))
    with pytest.raises(HostRejected):
        host.accept(
            "register", b"register " + EXEC.encode() + b" later", seal(b"x", KEY)
        )
    with pytest.raises(HostRejected):
        host.accept("register", _register(), "00" * 32)
    assert ledger.rows() == ()


def test_a_different_expiry_is_rejected_and_the_runtime_default_refuses_work() -> None:
    host, ledger = _host(runtime_available=True)
    first = _register()
    host.accept("register", first, seal(first, KEY))
    second = _register(NOW + 90)
    with pytest.raises(HostRejected):
        host.accept("register", second, seal(second, KEY))
    assert ledger.lookup(EXEC)[2] == NOW + 30
    closed, empty = _host()
    message = _register()
    with pytest.raises(HostRejected):
        closed.accept("register", message, seal(message, KEY))
    assert empty.rows() == ()


def test_cancel_and_complete_are_terminal_and_idempotent() -> None:
    host, ledger = _host(runtime_available=True)
    registered = _register()
    host.accept("register", registered, seal(registered, KEY))
    cancel = f"cancel {EXEC}".encode()
    host.accept("cancel", cancel, seal(cancel, KEY))
    host.accept("cancel", cancel, seal(cancel, KEY))
    assert ledger.lookup(EXEC) == (True, True, NOW + 30)
    complete = f"complete {OTHER}".encode()
    host.accept("complete", complete, seal(complete, KEY))
    host.accept("complete", complete, seal(complete, KEY))
    assert ledger.lookup(OTHER) == (True, True, 0)
    again = f"register {OTHER} {NOW + 30}".encode()
    with pytest.raises(HostRejected):
        host.accept("register", again, seal(again, KEY))


def test_health_is_authenticated_and_stays_a_simulation() -> None:
    host, _ledger = _host()
    with pytest.raises(HostRejected):
        host.health(b"health", "00" * 32)
    report = host.health(b"health", seal(b"health", KEY))
    assert report == HostHealth(1, "available", "unavailable", "simulation")
    simulated, _ledger = _host(runtime_available=True)
    assert simulated.health(b"health", seal(b"health", KEY)) == HostHealth(
        1, "available", "simulated", "simulation"
    )
    with pytest.raises(HostRejected):
        HostHealth(1, "available", "simulated", "per_run_microvm")
    assert KEY.hex() not in repr(host)
    assert repr(host) == "ReferenceHost()"


def test_an_unavailable_authority_writes_nothing() -> None:
    host, ledger = _host(runtime_available=True, authority_available=False)
    message = _register()
    with pytest.raises(HostRejected):
        host.accept("register", message, seal(message, KEY))
    cancel = f"cancel {EXEC}".encode()
    with pytest.raises(HostRejected):
        host.accept("cancel", cancel, seal(cancel, KEY))
    assert ledger.rows() == ()
    report = host.health(b"health", seal(b"health", KEY))
    assert report.authority == "unavailable"
    assert report.isolation == "simulation"
    status, selected = host.cleanup(((CID, EXEC),))
    assert (status, selected) == ("authority_unavailable", ())


def test_cleanup_fails_closed_without_a_runtime_and_selects_nothing() -> None:
    host, ledger = _host()
    cancel = f"cancel {EXEC}".encode()
    host.accept("cancel", cancel, seal(cancel, KEY))
    assert ledger.lookup(EXEC)[1] is True
    status, selected = host.cleanup(((CID, EXEC),))
    assert (status, selected) == ("runtime_unavailable", ())
    assert ledger.lookup(EXEC)[1] is True


def test_simulated_cleanup_is_idempotent_and_does_not_touch_a_runtime() -> None:
    host, ledger = _host(runtime_available=True)
    live = _register(NOW + 50)
    host.accept("register", live, seal(live, KEY))
    expired_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    expired = f"register {expired_id} {NOW - 1}".encode()
    # A past deadline is not a new registration.
    with pytest.raises(HostRejected):
        host.accept("register", expired, seal(expired, KEY))
    ledger.remember(expired_id, NOW - 1, cancelled=False)
    cancelled_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    cancelled = f"complete {cancelled_id}".encode()
    host.accept("complete", cancelled, seal(cancelled, KEY))
    rows = (
        (CID, EXEC),
        (CID, EXEC),
        (LIVE, expired_id),
        ("ef" * 6, cancelled_id),
        ("12" * 6, OTHER),
        ("not-a-container", OTHER),
        ("ab", EXEC),
    )
    before = ledger.rows()
    first = host.cleanup(rows)
    second = host.cleanup(rows)
    assert first == second == ("ok", (LIVE, "ef" * 6, "12" * 6))
    assert CID not in first[1]
    assert ledger.rows() == before
