"""Local-filesystem artifact store (ADR-009 single-node backend; ADR-013).

Keys are content-addressed: `<owner>/<org_id>/<repo_id>/sha256/<hex>`. Every part is
validated (owner word, canonical UUIDs, hex digest) before it touches a path, and the
resolved path must stay under the root, so a key can never traverse out of it.

- Immutable: an object is written to a private temporary file and hard-linked into place;
  linking never replaces an existing file. Files are read-only (0400), every directory
  level 0700. The root must be owned by this user and not group- or world-writable.
  Temporary files left by a crash (older than an hour) are swept at startup.
- Integrity: the SHA-256 is checked on every read, and on a put that finds the object
  already present.
- Idempotent: the same bytes in the same namespace give the same key and no new object.
  An object whose database transaction later rolls back stays as an orphan; a retry reuses
  it. `reconcile` reports orphans and missing keys. It does not delete them.
- Local files are plaintext. A production deployment must put this directory on an
  encrypted volume, or replace this adapter with encrypted object storage. This process
  does not encrypt, and it does not choose a retention duration.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from collections.abc import Collection
from pathlib import Path

from flakepilot.integrations.application.ports import (
    ArtifactRef,
    ArtifactStore,
    ArtifactStoreError,
)

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "ArtifactStoreError",
    "LocalFsArtifactStore",
    "reconcile",
    "root_problem",
]

STALE_TEMPORARY_SECONDS = 3600
SWEEP_MAX_FILES = 100_000

_logger = logging.getLogger("flakepilot.artifacts")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_OWNER = re.compile(r"[a-z]{2,10}")
_KEY = re.compile(rf"[a-z]{{2,10}}/{_UUID}/{_UUID}/sha256/[0-9a-f]{{64}}")


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def reconcile(
    root: Path, known: Collection[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Content-addressed files with no row, and known keys with no regular file.

    Symlinks and temporary files are ignored. Nothing is deleted.
    """
    problem = root_problem(root)
    if problem is not None:
        raise ValueError(f"the artifact root {problem}")
    base = root.resolve()
    present: set[str] = set()
    seen = 0
    for directory, dirnames, files in os.walk(base, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if not Path(directory, name).is_symlink()
        ]
        for name in files:
            seen += 1
            if seen > SWEEP_MAX_FILES:
                break
            if name.startswith(".tmp-"):
                continue
            path = Path(directory, name)
            if path.is_symlink():
                continue
            try:
                relative = path.resolve().relative_to(base).as_posix()
            except ValueError:
                continue
            if _KEY.fullmatch(relative):
                present.add(relative)
        if seen > SWEEP_MAX_FILES:
            break
    recorded = set(known)
    orphans = tuple(sorted(present - recorded))
    missing = tuple(sorted(key for key in recorded if key not in present))
    return orphans, missing


def root_problem(root: Path) -> str | None:
    """Why `root` can't hold artifacts, or None. Never mentions the path."""
    if not root.is_absolute() or not root.is_dir():
        return "must be an existing absolute directory"
    status = root.stat()
    if status.st_uid != os.getuid() or status.st_mode & 0o022:
        return "must be owned by this user and not writable by group or others"
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        return "must be readable and writable by this user"
    return None


class LocalFsArtifactStore:
    def __init__(self, root: Path) -> None:
        problem = root_problem(root)
        if problem is not None:
            raise ValueError(f"the artifact root {problem}")
        self._root = root.resolve()
        self._sweep_stale_temporaries()

    def _sweep_stale_temporaries(self) -> None:
        """Best effort: temporary files left by a crash mid-write. Bounded walk."""
        cutoff = time.time() - STALE_TEMPORARY_SECONDS
        removed = seen = 0
        for directory, _, files in os.walk(self._root):
            for name in files:
                seen += 1
                if seen > SWEEP_MAX_FILES:
                    break
                path = Path(directory, name)
                try:
                    if name.startswith(".tmp-") and path.lstat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            if seen > SWEEP_MAX_FILES:
                break
        if removed:
            _logger.info("removed %d stale temporary artifact files", removed)

    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef:
        if not (
            _OWNER.fullmatch(owner)
            and _canonical_uuid(org_id)
            and _canonical_uuid(repo_id)
        ):
            raise ArtifactStoreError("invalid_request")
        digest = hashlib.sha256(data).hexdigest()
        ref = ArtifactRef(
            f"{owner}/{org_id}/{repo_id}/sha256/{digest}", digest, len(data)
        )
        await asyncio.to_thread(self._write, ref, data)
        return ref

    async def get(self, ref: ArtifactRef) -> bytes:
        return await asyncio.to_thread(self._read, ref)

    def _path(self, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise ArtifactStoreError("invalid_request")
        path = self._root.joinpath(*key.split("/"))
        if not path.resolve().is_relative_to(self._root):
            raise ArtifactStoreError("invalid_request")
        return path

    def _write(self, ref: ArtifactRef, data: bytes) -> None:
        path = self._path(ref.key)
        if path.exists():
            self._read(ref)  # present already: it must be intact
            return
        self._make_directories(path.parent)
        temporary = path.parent / f".tmp-{uuid.uuid4().hex}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # never replaces an existing object
            except FileExistsError:
                self._read(ref)  # a concurrent writer stored it first
        finally:
            temporary.unlink(missing_ok=True)

    def _make_directories(self, directory: Path) -> None:
        """Every level 0700 (mkdir's `parents` would give intermediates the umask)."""
        current = self._root
        for part in directory.relative_to(self._root).parts:
            current = current / part
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                pass

    def _read(self, ref: ArtifactRef) -> bytes:
        path = self._path(ref.key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ArtifactStoreError("not_found") from None
        digest = hashlib.sha256(data).hexdigest()
        if not (
            digest == ref.sha256 == ref.key.rsplit("/", 1)[1] and len(data) == ref.size
        ):
            raise ArtifactStoreError("integrity")
        return data
