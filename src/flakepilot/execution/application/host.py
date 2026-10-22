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
        """`(registered, terminal, expires)`. Unknown ids are not registered.

        `terminal` is true for both `cancelled` and `completed`. `phase` tells
        those marks apart. A reaper may remove either mark. It must not treat
        them as the same audit event.
        """
        raise NotImplementedError


_OPEN = 0
_CANCELLED = 1
_COMPLETED = 2
_PHASE = {0: "open", 1: "cancelled", 2: "completed"}
_MARK = {"open": _OPEN, "cancelled": _CANCELLED, "completed": _COMPLETED}


class MemoryLedger(ExecutionAuthority):
    """Authority kept in this process. `register` is the host-local write.

    `open`, `cancelled`, and `completed` are distinct. The first terminal mark
    stays. A later mark of the other kind is rejected. Cancel of an unknown id
    is a tombstone. Complete of an unknown id writes nothing.
    """

    def __init__(self) -> None:
        self._rows: dict[str, tuple[int, int]] = {}

    def register(self, execution_id: str, expires: int) -> None:
        if not execution_id_ok(execution_id) or not expires_ok(expires):
            raise HostRejected
        current = self._rows.get(execution_id)
        if current is None:
            self._rows[execution_id] = (expires, _OPEN)
            return
        stored, code = current
        if code != _OPEN or stored != expires:
            raise HostRejected
        return

    def cancel(self, execution_id: str) -> None:
        if not execution_id_ok(execution_id):
            raise HostRejected
        current = self._rows.get(execution_id)
        if current is None:
            self._rows[execution_id] = (0, _CANCELLED)
            return
        expires, code = current
        if code == _CANCELLED:
            return
        if code != _OPEN:
            raise HostRejected
        self._rows[execution_id] = (expires, _CANCELLED)

    def complete(self, execution_id: str) -> None:
        """Close an open registration. An unknown id is not a completion."""
        if not execution_id_ok(execution_id):
            raise HostRejected
        current = self._rows.get(execution_id)
        if current is None:
            raise HostRejected
        expires, code = current
        if code == _COMPLETED:
            return
        if code != _OPEN:
            raise HostRejected
        self._rows[execution_id] = (expires, _COMPLETED)

    def phase(self, execution_id: str) -> str:
        """`absent`, `open`, `cancelled`, or `completed`."""
        current = self._rows.get(execution_id)
        if current is None:
            return "absent"
        return _PHASE[current[1]]

    def lookup(self, execution_id: str) -> tuple[bool, bool, int]:
        current = self._rows.get(execution_id)
        if current is None:
            return False, False, 0
        expires, code = current
        return True, code != _OPEN, expires

    def rows(self) -> tuple[tuple[str, int, int], ...]:
        """`(execution id, expires, code)`. Code is 0 open, 1 cancelled, 2 completed."""
        return tuple(
            (execution_id, expires, code)
            for execution_id, (expires, code) in sorted(self._rows.items())
        )

    def remember(
        self,
        execution_id: str,
        expires: int,
        *,
        cancelled: bool | None = None,
        mark: str | None = None,
    ) -> None:
        """Reload one row the host file already stored. Not a handoff."""
        if not execution_id_ok(execution_id) or not expires_ok(expires):
            raise LedgerError
        if mark is not None:
            if cancelled is not None or mark not in _MARK:
                raise LedgerError
            code = _MARK[mark]
        elif isinstance(cancelled, bool):
            code = _CANCELLED if cancelled else _OPEN
        else:
            raise LedgerError
        self._rows[execution_id] = (expires, code)


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
