"""Version 1 host contract, exercised in-process. This is not a network transport.

Messages are `register <execution id> <unix expiry>`, `cancel <execution id>`,
`complete <execution id>`, and `health`. The mac is HMAC-SHA256 of those exact
bytes. `cleanup` reads the ledger and selects ids. It does not start or stop
a runtime.

The host ledger key stays in this object. The control plane does not construct
`ReferenceHost`, and nothing here starts a child process. `runtime_available`
only means this simulation may record a registration. Health isolation is
always `simulation`. A missing authority or runtime does not select cleanup
ids and does not accept new work.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from flakepilot.execution.application.host import (
    HostGate,
    HostRejected,
    LedgerError,
    MemoryLedger,
)
from flakepilot.execution.domain.plane import (
    container_action,
    execution_id_ok,
    expires_ok,
)

__all__ = ["OPERATIONS", "PROTOCOL", "HostHealth", "ReferenceHost"]

PROTOCOL = 1
OPERATIONS = frozenset({"register", "cancel", "complete", "health", "cleanup"})
_MAX_MESSAGE = 80


@dataclass(frozen=True)
class HostHealth:
    """What an authenticated caller may learn. Isolation cannot be promoted."""

    protocol: int
    authority: str
    runtime: str
    isolation: str

    def __post_init__(self) -> None:
        if (
            self.protocol != PROTOCOL
            or self.authority not in {"available", "unavailable"}
            or self.runtime not in {"simulated", "unavailable"}
            or self.isolation != "simulation"
        ):
            raise HostRejected


class ReferenceHost:
    """Authenticated ledger operations for tests of the host contract."""

    def __init__(
        self,
        ledger: MemoryLedger,
        key: bytes,
        *,
        now: int,
        runtime_available: bool = False,
        authority_available: bool = True,
        previous_keys: tuple[bytes, ...] = (),
    ) -> None:
        if (
            not isinstance(ledger, MemoryLedger)
            or not isinstance(now, int)
            or isinstance(now, bool)
            or not expires_ok(now)
            or not isinstance(runtime_available, bool)
            or not isinstance(authority_available, bool)
            or not _key_set(key, previous_keys)
        ):
            raise HostRejected
        self._ledger = ledger
        self._key = key
        self._verify_keys = (key, *previous_keys)
        self._gate = HostGate(ledger, key)
        self._now = now
        self._runtime = runtime_available
        self._authority = authority_available

    def __repr__(self) -> str:
        return "ReferenceHost()"

    def health(self, message: bytes, mac_hex: str) -> HostHealth:
        _parts("health", message)
        if not _mac_ok(message, mac_hex, self._verify_keys):
            raise HostRejected
        return HostHealth(
            PROTOCOL,
            "available" if self._authority else "unavailable",
            "simulated" if self._runtime else "unavailable",
            "simulation",
        )

    def accept(self, operation: str, message: bytes, mac_hex: str) -> None:
        """Record one allowlisted change. A bad request writes nothing."""
        if operation not in {"register", "cancel", "complete"}:
            raise HostRejected
        parts = _parts(operation, message)
        if not _mac_ok(message, mac_hex, self._verify_keys):
            raise HostRejected
        if not self._authority:
            raise HostRejected
        if operation == "register":
            if not self._runtime:
                raise HostRejected
            expires = int(parts[2])
            if expires <= self._now:
                raise HostRejected
            self._ledger.register(parts[1], expires)
            return
        if operation == "cancel":
            self._ledger.cancel(parts[1])
            return
        self._ledger.complete(parts[1])

    def cleanup(
        self, containers: tuple[tuple[str, str], ...]
    ) -> tuple[str, tuple[str, ...]]:
        """`(status, selected ids)`. Selection is not removal.

        `authority_unavailable` and `runtime_unavailable` select nothing.
        A ledger that cannot be read selects nothing. `ok` lists ids the
        ledger says to remove. The same input selects the same ids, and the
        ledger rows stay as they were.
        """
        if not isinstance(containers, tuple):
            raise HostRejected
        if not self._authority:
            return ("authority_unavailable", ())
        if not self._runtime:
            return ("runtime_unavailable", ())
        selected: list[str] = []
        seen: set[str] = set()
        for pair in containers:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not isinstance(pair[1], str)
                or not _hex_id(pair[0])
                or not execution_id_ok(pair[1])
                or pair[0] in seen
            ):
                continue
            try:
                registered, cancelled, expires = self._ledger.lookup(pair[1])
            except LedgerError:
                return ("authority_unavailable", ())
            action = container_action(
                registered=registered,
                cancelled=cancelled,
                expires=expires,
                now=self._now,
            )
            if action != "remove":
                continue
            seen.add(pair[0])
            selected.append(pair[0])
        return ("ok", tuple(selected))


def _parts(operation: str, message: bytes) -> tuple[str, ...]:
    if (
        operation not in OPERATIONS
        or not isinstance(message, bytes)
        or len(message) > _MAX_MESSAGE
        or not message.isascii()
        or any(character in message for character in b"\n\r\x00")
    ):
        raise HostRejected
    parts = message.decode("ascii").split(" ")
    if operation == "health" and parts == ["health"]:
        return ("health",)
    if (
        operation == "register"
        and len(parts) == 3
        and parts[0] == "register"
        and parts[2].isdigit()
        and len(parts[2]) <= 10
        and execution_id_ok(parts[1])
        and expires_ok(int(parts[2]))
    ):
        return (parts[0], parts[1], parts[2])
    if (
        operation in {"cancel", "complete"}
        and len(parts) == 2
        and parts[0] == operation
        and execution_id_ok(parts[1])
    ):
        return (parts[0], parts[1])
    raise HostRejected


def _key_set(key: bytes, previous: tuple[bytes, ...]) -> bool:
    if not isinstance(previous, tuple) or not isinstance(key, bytes) or len(key) < 32:
        return False
    seen = {key}
    for item in previous:
        if not isinstance(item, bytes) or len(item) < 32 or item in seen:
            return False
        seen.add(item)
    return True


def _mac_ok(message: bytes, mac_hex: str, keys: tuple[bytes, ...]) -> bool:
    if (
        not isinstance(mac_hex, str)
        or len(mac_hex) != 64
        or any(character not in "0123456789abcdef" for character in mac_hex)
        or not keys
    ):
        return False
    presented = bytes.fromhex(mac_hex)
    matched = False
    for key in keys:
        expected = hmac.new(key, message, hashlib.sha256).digest()
        matched = hmac.compare_digest(expected, presented) or matched
    return matched


def _hex_id(value: str) -> bool:
    return 12 <= len(value) <= 64 and all(
        character in "0123456789abcdef" for character in value
    )
