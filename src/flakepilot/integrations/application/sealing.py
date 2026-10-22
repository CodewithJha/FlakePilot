"""Encryption boundary for artifact bytes. No key lives in this module.

Production has no key manager. `UnconfiguredSealer` refuses to seal or open.
It does not pretend plaintext is ciphertext.
"""

from typing import Protocol

from flakepilot.integrations.application.ports import ArtifactStoreError

__all__ = ["ArtifactSealer", "UnconfiguredSealer"]


class ArtifactSealer(Protocol):
    """Turn artifact bytes into a form that is useless without the key, and back."""

    def seal(self, data: bytes) -> bytes: ...

    def open(self, blob: bytes) -> bytes: ...


class UnconfiguredSealer:
    """No key is configured. Seal and open both fail closed."""

    def seal(self, data: bytes) -> bytes:
        del data
        raise ArtifactStoreError("encryption_unavailable")

    def open(self, blob: bytes) -> bytes:
        del blob
        raise ArtifactStoreError("encryption_unavailable")
