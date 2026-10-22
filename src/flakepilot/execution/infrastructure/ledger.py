"""Host-local ledger file. The worker does not open it.

The file is the authority the reaper reads. It is not mounted into a
container. A symlink, another user's file, a missing file, an empty file, or
a mode other than `0600` is refused and is not treated as an empty ledger.
Register and cancel hold an exclusive flock on a sibling lock file across the
reload and the replace. The lock is not the ledger inode, because replace
creates a new inode. A lock that cannot be taken leaves the previous file
unchanged. No key is read from the environment.
"""

from __future__ import annotations

import fcntl
import os
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flakepilot.execution.application.host import LedgerError, MemoryLedger

__all__ = ["FileLedger"]

_MAX_BYTES = 8192
_LOCK_WAIT_SECONDS = 1.0


class FileLedger(MemoryLedger):
    """Load and store `MemoryLedger` at one path. Writes are mode `0600`."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        if not isinstance(path, Path) or not path.is_absolute():
            raise LedgerError
        self._path = path

    def register(self, execution_id: str, expires: int) -> None:
        with self._locked(exclusive=True):
            self._reload(missing_ok=True)
            super().register(execution_id, expires)
            self._store()

    def cancel(self, execution_id: str) -> None:
        with self._locked(exclusive=True):
            self._reload(missing_ok=True)
            super().cancel(execution_id)
            self._store()

    def lookup(self, execution_id: str) -> tuple[bool, bool, int]:
        with self._locked(exclusive=False):
            self._reload(missing_ok=False)
            return super().lookup(execution_id)

    def _reload(self, *, missing_ok: bool) -> None:
        self._rows = {}
        if not self._path.exists():
            if self._path.is_symlink():
                raise LedgerError
            if missing_ok:
                return
            raise LedgerError
        text = _read(self._path)
        if text.strip() == "":
            raise LedgerError
        seen: set[str] = set()
        for line in text.splitlines():
            parts = line.split(" ")
            if len(parts) != 3 or parts[2] not in ("0", "1") or not parts[1].isdigit():
                raise LedgerError
            if parts[0] in seen or len(parts[1]) > 10:
                raise LedgerError
            seen.add(parts[0])
            self.remember(parts[0], int(parts[1]), cancelled=parts[2] == "1")
        if not seen:
            raise LedgerError

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
        try:
            descriptor = os.open(temporary, flags, 0o600)
        except OSError:
            raise LedgerError from None
        try:
            _write_all(descriptor, blob)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        except OSError:
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise LedgerError from None
        os.close(descriptor)
        try:
            os.replace(temporary, self._path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise LedgerError from None
        try:
            os.chmod(self._path, 0o600)
            _fsync_directory(self._path.parent)
        except OSError:
            raise LedgerError from None

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = _open_lock(_lock_path(self._path))
        try:
            _flock(descriptor, exclusive=exclusive)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _lock_path(path: Path) -> Path:
    """Sibling of the ledger. Replacing the ledger must not replace this inode."""
    return path.with_name("." + path.name + ".lock")


def _open_lock(path: Path) -> int:
    if path.is_symlink():
        raise LedgerError
    nofollow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        old = os.umask(0o077)
        try:
            try:
                descriptor = os.open(
                    path, os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow, 0o600
                )
            except FileExistsError:
                descriptor = -1
        finally:
            os.umask(old)
        if descriptor < 0:
            if path.is_symlink():
                raise LedgerError
            descriptor = os.open(path, os.O_RDWR | nofollow)
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or mode & 0o077:
            raise LedgerError
        os.fchmod(descriptor, 0o600)
        return descriptor
    except LedgerError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise LedgerError from None


def _flock(descriptor: int, *, exclusive: bool) -> None:
    kind = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, kind | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LedgerError from None
            time.sleep(0.01)
        except OSError:
            raise LedgerError from None


def _write_all(descriptor: int, blob: bytes) -> None:
    offset = 0
    while offset < len(blob):
        count = os.write(descriptor, blob[offset:])
        if count <= 0:
            raise OSError
        offset += count


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
