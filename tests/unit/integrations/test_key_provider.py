"""Key-provider failures stay closed. No production key is stored here."""

import asyncio
import secrets
from pathlib import Path

import pytest

from flakepilot.integrations.application.ports import ArtifactRef, ArtifactStoreError
from flakepilot.integrations.application.sealing import UnavailableKeyProvider
from flakepilot.integrations.infrastructure.aes_gcm import ProviderBackedSealer
from flakepilot.integrations.infrastructure.sealed_store import SealedArtifactStore

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SECRET = b"secret-artifact-bytes"
_ROOT = Path(__file__).resolve().parents[3]


class _Memory:
    def __init__(self) -> None:
        self.puts = 0
        self.blob = b""

    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef:
        del owner, org_id, repo_id
        self.puts += 1
        self.blob = data
        digest = "ab" * 32
        return ArtifactRef(f"ti/{ORG}/{REPO}/sha256/{digest}", digest, len(data))

    async def get(self, ref: ArtifactRef) -> bytes:
        del ref
        return self.blob


class _Map:
    def __init__(self, keys: dict[str, bytes], current: str) -> None:
        self._keys = keys
        self._current = current

    def seal_key(self) -> tuple[str, bytes]:
        return self._current, self._keys[self._current]

    def open_key(self, key_id: str) -> bytes:
        try:
            return self._keys[key_id]
        except KeyError:
            raise ArtifactStoreError("integrity") from None


class _Short:
    def seal_key(self) -> tuple[str, bytes]:
        return "k1", b"short"

    def open_key(self, key_id: str) -> bytes:
        del key_id
        return b"short"


class _Boom:
    def seal_key(self) -> tuple[str, bytes]:
        raise RuntimeError("super-secret-key-material")

    def open_key(self, key_id: str) -> bytes:
        del key_id
        raise RuntimeError("super-secret-key-material")


def test_an_unavailable_provider_does_not_write() -> None:
    inner = _Memory()
    store = SealedArtifactStore(inner, ProviderBackedSealer(UnavailableKeyProvider()))
    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=SECRET))
    assert excinfo.value.kind == "encryption_unavailable"
    assert inner.puts == 0


def test_an_invalid_or_leaking_provider_fails_closed() -> None:
    sealer = ProviderBackedSealer(_Short())
    with pytest.raises(ArtifactStoreError) as short:
        sealer.seal(SECRET)
    assert short.value.kind == "encryption_unavailable"
    boom = ProviderBackedSealer(_Boom())
    with pytest.raises(ArtifactStoreError) as leaked:
        boom.seal(SECRET)
    assert leaked.value.kind == "encryption_unavailable"
    assert "super-secret-key-material" not in str(leaked.value)
    assert repr(sealer) == "ProviderBackedSealer()"


def test_revocation_refuses_open_and_the_app_does_not_construct_the_provider() -> None:
    key = secrets.token_bytes(32)
    sealer = ProviderBackedSealer(_Map({"k1": key}, "k1"))
    blob = sealer.seal(SECRET)
    assert SECRET not in blob
    assert sealer.open(blob) == SECRET
    retired = ProviderBackedSealer(_Map({}, "k2"))
    with pytest.raises(ArtifactStoreError) as missing:
        retired.open(blob)
    assert missing.value.kind == "integrity"
    app = (_ROOT / "src/flakepilot/interface/composition_root/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "ProviderBackedSealer" not in app
    assert "LocalFsArtifactStore" in app
