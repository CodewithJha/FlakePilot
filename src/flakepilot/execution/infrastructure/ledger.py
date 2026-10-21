"""Host-local ledger file. The worker does not open it.

The file is the authority the reaper reads. It is not mounted into a
container. A symlink, another user's file, or a mode other than `0600` is
refused and is not treated as an empty ledger. No key is read from the
environment.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from flakepilot.execution.application.host import LedgerError, MemoryLedger

__all__ = ["FileLedger"]

_MAX_BYTES = 8192


class FileLedger(MemoryLedger):
    """Load and store `MemoryLedger` at one path. Writes are mode `0600`."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        if not isinstance(path, Path) or not path.is_absolute():
            raise LedgerError
        self._path = path

    def register(self, execution_id: str, expires: int) -> None:
        self._reload()
        super().register(execution_id, expires)
        self._store()

    def cancel(self, execution_id: str) -> None:
        self._reload()
        super().cancel(execution_id)
        self._store()

    def lookup(self, execution_id: str) -> tuple[bool, bool, int]:
        self._reload()
        return super().lookup(execution_id)

    def _reload(self) -> None:
        self._rows = {}
        if not self._path.exists():
            if self._path.is_symlink():
                raise LedgerError
            return
        text = _read(self._path)
        seen: set[str] = set()
        for line in text.splitlines():
            parts = line.split(" ")
            if len(parts) != 3 or parts[2] not in ("0", "1") or not parts[1].isdigit():
                raise LedgerError
            if parts[0] in seen or len(parts[1]) > 10:
                raise LedgerError
            seen.add(parts[0])
            self.remember(parts[0], int(parts[1]), cancelled=parts[2] == "1")

    def _store(self) -> None:
        if self._path.is_symlink():
            raise LedgerError
        blob = "".join(
            f"{execution_id} {expires} {int(cancelled)}\n"
            for execution_id, expires, cancelled in self.rows()
        ).encode("ascii")
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        if temporary.exists() or temporary.is_symlink():
            raise LedgerError
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.write(descriptor, blob)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, self._path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise LedgerError from None
        os.chmod(self._path, 0o600)


def _read(path: Path) -> str:
    if path.is_symlink():
        raise LedgerError
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LedgerError from None
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or mode & 0o077:
            raise LedgerError
        data = os.read(descriptor, _MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > _MAX_BYTES:
        raise LedgerError
    try:
        return data.decode("ascii")
    except UnicodeError:
        raise LedgerError from None
