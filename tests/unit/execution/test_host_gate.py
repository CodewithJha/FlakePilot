"""The host accepts one mac and two fields. It does not start a runtime."""

import pytest

from flakepilot.execution.application.host import (
    HostGate,
    HostRejected,
    MemoryLedger,
    seal,
)

EXECUTION = "11111111-1111-1111-1111-111111111111"
KEY = bytes(range(32))


def test_a_register_is_idempotent_and_a_later_expiry_is_refused() -> None:
    ledger = MemoryLedger()
    gate = HostGate(ledger, KEY)
    message = f"register {EXECUTION} 100".encode()
    gate.accept(message, seal(message, KEY))
    gate.accept(message, seal(message, KEY))
    extended = f"register {EXECUTION} 999".encode()
    with pytest.raises(HostRejected, match="rejected"):
        gate.accept(extended, seal(extended, KEY))
    assert ledger.lookup(EXECUTION) == (True, False, 100)


def test_a_bad_mac_or_a_smuggled_url_changes_nothing() -> None:
    ledger = MemoryLedger()
    gate = HostGate(ledger, KEY)
    message = f"register {EXECUTION} 100".encode()
    with pytest.raises(HostRejected, match="rejected"):
        gate.accept(message, "0" * 64)
    smuggled = f"register {EXECUTION} 100 postgresql://db/secret".encode()
    with pytest.raises(HostRejected, match="rejected") as info:
        gate.accept(smuggled, seal(smuggled, KEY))
    assert "secret" not in str(info.value)
    assert "postgresql" not in repr(gate)
    assert ledger.lookup(EXECUTION) == (False, False, 0)


def test_cancel_is_authenticated_and_blocks_a_later_register() -> None:
    ledger = MemoryLedger()
    gate = HostGate(ledger, KEY)
    register = f"register {EXECUTION} 500".encode()
    gate.accept(register, seal(register, KEY))
    cancel = f"cancel {EXECUTION}".encode()
    gate.accept(cancel, seal(cancel, KEY))
    gate.accept(cancel, seal(cancel, KEY))
    with pytest.raises(HostRejected):
        gate.accept(register, seal(register, KEY))
    assert ledger.lookup(EXECUTION) == (True, True, 500)


def test_a_short_key_is_refused() -> None:
    with pytest.raises(HostRejected):
        HostGate(MemoryLedger(), b"short")
