"""Narrow handoff the execution host can accept. The worker does not call this.

A message is `register <execution id> <unix expiry>` or `cancel <execution id>`.
The mac is HMAC-SHA256 of those exact bytes. There is no database URL, no
GitHub key, and no second field. This module does not open a socket, start a
runtime, or read the process environment. No key is stored in source.
"""

from __future__ import annotations

import hashlib
import hmac

from flakepilot.execution.domain.plane import execution_id_ok, expires_ok

__all__ = [
    "ExecutionAuthority",
    "HostGate",
    "HostRejected",
    "LedgerError",
    "MemoryLedger",
]

_MAX_MESSAGE = 80


class HostRejected(Exception):
    """The handoff was refused. The message does not echo the input or the key."""

    def __init__(self) -> None:
        super().__init__("rejected")


class LedgerError(Exception):
    """The host ledger file could not be trusted. Nothing was decided from it."""

    def __init__(self) -> None:
        super().__init__("rejected")


class ExecutionAuthority:
    """What the reaper is allowed to know. Not a database row."""

    def lookup(self, execution_id: str) -> tuple[bool, bool, int]:
        """`(registered, cancelled, expires)`. Unknown ids are not registered."""
        raise NotImplementedError


class MemoryLedger(ExecutionAuthority):
    """Authority kept in this process. `register` is the host-local write."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[int, bool]] = {}

    def register(self, execution_id: str, expires: int) -> None:
        if not execution_id_ok(execution_id) or not expires_ok(expires):
            raise HostRejected
        current = self._rows.get(execution_id)
        if current is None:
            self._rows[execution_id] = (expires, False)
            return
        stored, cancelled = current
        if cancelled or stored != expires:
            raise HostRejected
        return

    def cancel(self, execution_id: str) -> None:
        if not execution_id_ok(execution_id):
            raise HostRejected
        current = self._rows.get(execution_id)
        expires = 0 if current is None else current[0]
        self._rows[execution_id] = (expires, True)

    def lookup(self, execution_id: str) -> tuple[bool, bool, int]:
        current = self._rows.get(execution_id)
        if current is None:
            return False, False, 0
        expires, cancelled = current
        return True, cancelled, expires

    def rows(self) -> tuple[tuple[str, int, bool], ...]:
        return tuple(
            (execution_id, expires, cancelled)
            for execution_id, (expires, cancelled) in sorted(self._rows.items())
        )

    def remember(self, execution_id: str, expires: int, *, cancelled: bool) -> None:
        """Reload one row the host file already stored. Not a handoff."""
        if (
            not execution_id_ok(execution_id)
            or not expires_ok(expires)
            or not isinstance(cancelled, bool)
        ):
            raise LedgerError
        self._rows[execution_id] = (expires, cancelled)


class HostGate:
    """Check one mac, then update the ledger. The reaper has no key."""

    def __init__(self, ledger: MemoryLedger, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise HostRejected
        self._ledger = ledger
        self._key = key

    def __repr__(self) -> str:
        return "HostGate()"

    def accept(self, message: bytes, mac_hex: str) -> None:
        if not _message_ok(message) or not _mac_ok(message, mac_hex, self._key):
            raise HostRejected
        text = message.decode("ascii")
        parts = text.split(" ")
        if len(parts) == 3 and parts[0] == "register" and parts[2].isdigit():
            if len(parts[2]) > 10:
                raise HostRejected
            self._ledger.register(parts[1], int(parts[2]))
            return
        if len(parts) == 2 and parts[0] == "cancel":
            self._ledger.cancel(parts[1])
            return
        raise HostRejected


def seal(message: bytes, key: bytes) -> str:
    """Hex mac for tests and for the host process that holds the key."""
    if not isinstance(key, bytes) or len(key) < 32:
        raise HostRejected
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _message_ok(message: object) -> bool:
    return (
        isinstance(message, bytes)
        and len(message) <= _MAX_MESSAGE
        and message.isascii()
        and b"\n" not in message
        and b"\r" not in message
        and b"\x00" not in message
    )


def _mac_ok(message: bytes, mac_hex: str, key: bytes) -> bool:
    if (
        not isinstance(mac_hex, str)
        or len(mac_hex) != 64
        or any(character not in "0123456789abcdef" for character in mac_hex)
    ):
        return False
    expected = hmac.new(key, message, hashlib.sha256).digest()
    return hmac.compare_digest(expected, bytes.fromhex(mac_hex))
