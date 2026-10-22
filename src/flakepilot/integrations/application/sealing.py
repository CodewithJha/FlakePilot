"""Encryption boundary for artifact bytes. No key lives in this module.

Production has no key manager. `UnconfiguredSealer` refuses to seal or open.
It does not pretend plaintext is ciphertext.
"""

from typing import Protocol

from flakepilot.integrations.application.ports import ArtifactStoreError

__all__ = [
    "ArtifactKeyProvider",
    "ArtifactSealer",
    "UnavailableKeyProvider",
    "UnconfiguredSealer",
]


class ArtifactSealer(Protocol):
    """Turn artifact bytes into a form that is useless without the key, and back."""

    def seal(self, data: bytes) -> bytes: ...

    def open(self, blob: bytes) -> bytes: ...


class ArtifactKeyProvider(Protocol):
    """Keys from a manager outside this process. No key is stored here.

    `seal_key` returns the current `(key id, 32-byte key)`. `open_key` returns
    the key for a blob id, including a retired id that has not been destroyed.
    Unavailable and invalid results fail closed. Revocation refuses `open_key`.
    """

    def seal_key(self) -> tuple[str, bytes]: ...

    def open_key(self, key_id: str) -> bytes: ...


class UnconfiguredSealer:
    """No key is configured. Seal and open both fail closed."""

    def seal(self, data: bytes) -> bytes:
        del data
        raise ArtifactStoreError("encryption_unavailable")

    def open(self, blob: bytes) -> bytes:
        del blob
        raise ArtifactStoreError("encryption_unavailable")


class UnavailableKeyProvider:
    """No external key manager is configured."""

    def seal_key(self) -> tuple[str, bytes]:
        raise ArtifactStoreError("encryption_unavailable")

    def open_key(self, key_id: str) -> bytes:
        del key_id
        raise ArtifactStoreError("encryption_unavailable")
