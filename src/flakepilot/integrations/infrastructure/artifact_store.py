"""Local-filesystem artifact store (ADR-009 single-node backend; ADR-013).

Keys are content-addressed: `<owner>/<org_id>/<repo_id>/sha256/<hex>`. Every part is
validated (owner word, canonical UUIDs, hex digest) before it touches a path, and the
resolved path must stay under the root, so a key can never traverse out of it.

- Immutable: an object is written to a private temporary file and hard-linked into place;
  linking never replaces an existing file. Files are read-only (0400), directories 0700.
- Integrity: the SHA-256 is checked on every read, and on a put that finds the object
  already present.
- Idempotent: the same bytes in the same namespace give the same key and no new object.
  An object whose database transaction later rolls back stays as an orphan; a retry reuses
  it. Orphans and retention deletion are cleaned up with the retention job (deferred).
- Not yet: encryption at rest, deletion, scoped read handles (ADR-013).
"""

import asyncio
import hashlib
import os
import re
import uuid
from pathlib import Path

from flakepilot.integrations.application.ports import ArtifactRef, ArtifactStoreError

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_OWNER = re.compile(r"[a-z]{2,10}")
_KEY = re.compile(rf"[a-z]{{2,10}}/{_UUID}/{_UUID}/sha256/[0-9a-f]{{64}}")


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class LocalFsArtifactStore:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("the artifact root must be an existing absolute directory")
        self._root = root.resolve()

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
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
