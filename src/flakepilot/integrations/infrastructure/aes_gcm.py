"""Versioned AES-GCM for artifact bytes. No key is stored in this module.

A blob is version 1, a key identifier, a 12-byte nonce, and AES-GCM
ciphertext including its tag. `seal` uses `current_id`. `open` selects the
key by the identifier in the blob, so a retired key still decrypts while it
remains in the map. An unknown identifier, a wrong key, or a failed tag
raises `integrity`. A blob that is not this layout raises `invalid_request`.

The caller supplies the keys. This module does not read the environment, log
key material, or choose where the keys live. The composition root does not
construct this sealer. Ciphertext in a unit test is not deployment encryption
at rest: that still needs a key manager outside the execution plane.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from flakepilot.integrations.application.ports import ArtifactStoreError
from flakepilot.integrations.application.sealing import ArtifactKeyProvider

__all__ = ["AesGcmSealer", "ProviderBackedSealer"]

_VERSION = 1
_NONCE_LEN = 12
_TAG_LEN = 16


class AesGcmSealer:
    """Authenticated encryption bound to a key identifier."""

    def __init__(self, keys: Mapping[str, bytes], current_id: str) -> None:
        if not isinstance(keys, Mapping) or current_id not in keys:
            raise ArtifactStoreError("encryption_unavailable")
        stored: dict[str, bytes] = {}
        for key_id, key in keys.items():
            if not _id_ok(key_id) or not isinstance(key, bytes) or len(key) != 32:
                raise ArtifactStoreError("encryption_unavailable")
            stored[key_id] = bytes(key)
        if not _id_ok(current_id):
            raise ArtifactStoreError("encryption_unavailable")
        self._keys = stored
        self._current = current_id

    def __repr__(self) -> str:
        return "AesGcmSealer()"

    def seal(self, data: bytes) -> bytes:
        if not isinstance(data, bytes):
            raise ArtifactStoreError("invalid_request")
        key_id = self._current.encode("ascii")
        nonce = os.urandom(_NONCE_LEN)
        ciphertext = AESGCM(self._keys[self._current]).encrypt(
            nonce, data, _aad(self._current)
        )
        return bytes((_VERSION, len(key_id))) + key_id + nonce + ciphertext

    def open(self, blob: bytes) -> bytes:
        key_id, nonce, ciphertext = _parse(blob)
        key = self._keys.get(key_id)
        if key is None:
            raise ArtifactStoreError("integrity")
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, _aad(key_id))
        except InvalidTag:
            raise ArtifactStoreError("integrity") from None


class ProviderBackedSealer:
    """Ask a key provider on every seal and open. The provider holds the bytes.

    This sealer is not constructed by the composition root. A provider that is
    down, or that returns a key that is not 32 bytes, fails closed.
    """

    def __init__(self, provider: ArtifactKeyProvider) -> None:
        self._provider = provider

    def __repr__(self) -> str:
        return "ProviderBackedSealer()"

    def seal(self, data: bytes) -> bytes:
        key_id, key = _seal_material(self._provider)
        return AesGcmSealer({key_id: key}, key_id).seal(data)

    def open(self, blob: bytes) -> bytes:
        key_id, _nonce, _ciphertext = _parse(blob)
        key = _open_material(self._provider, key_id)
        return AesGcmSealer({key_id: key}, key_id).open(blob)


def _seal_material(provider: ArtifactKeyProvider) -> tuple[str, bytes]:
    try:
        key_id, key = provider.seal_key()
    except ArtifactStoreError:
        raise
    except Exception:  # noqa: BLE001 - provider errors must not carry key text
        raise ArtifactStoreError("encryption_unavailable") from None
    if not _id_ok(key_id) or not isinstance(key, bytes) or len(key) != 32:
        raise ArtifactStoreError("encryption_unavailable")
    return key_id, key


def _open_material(provider: ArtifactKeyProvider, key_id: str) -> bytes:
    try:
        key = provider.open_key(key_id)
    except ArtifactStoreError:
        raise
    except Exception:  # noqa: BLE001 - provider errors must not carry key text
        raise ArtifactStoreError("encryption_unavailable") from None
    if not isinstance(key, bytes) or len(key) != 32:
        raise ArtifactStoreError("encryption_unavailable")
    return key


def _aad(key_id: str) -> bytes:
    return b"flakepilot-artifact-v1\n" + key_id.encode("ascii")


def _id_ok(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 32 or not value.isascii():
        return False
    return value[0] in "abcdefghijklmnopqrstuvwxyz" and all(
        character in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value
    )


def _parse(blob: bytes) -> tuple[str, bytes, bytes]:
    if not isinstance(blob, bytes) or len(blob) < 2 + 1 + _NONCE_LEN + _TAG_LEN:
        raise ArtifactStoreError("invalid_request")
    if blob[0] != _VERSION:
        raise ArtifactStoreError("invalid_request")
    key_len = blob[1]
    end = 2 + key_len
    if key_len < 1 or key_len > 32 or len(blob) < end + _NONCE_LEN + _TAG_LEN:
        raise ArtifactStoreError("invalid_request")
    try:
        key_id = blob[2:end].decode("ascii")
    except UnicodeDecodeError:
        raise ArtifactStoreError("invalid_request") from None
    if not _id_ok(key_id):
        raise ArtifactStoreError("invalid_request")
    return key_id, blob[end : end + _NONCE_LEN], blob[end + _NONCE_LEN :]
