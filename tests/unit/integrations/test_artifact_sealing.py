"""Encryption and retention stay closed. These tests do not invent a key."""

import asyncio
from pathlib import Path

import pytest

from flakepilot.integrations.application.ports import ArtifactRef, ArtifactStoreError
from flakepilot.integrations.application.retention import select_for_deletion
from flakepilot.integrations.application.sealing import UnconfiguredSealer
from flakepilot.integrations.infrastructure.sealed_store import SealedArtifactStore

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DIGEST = "ab" * 32


class _Memory:
    def __init__(self) -> None:
        self.puts = 0
        self.gets = 0

    async def put(
        self, *, owner: str, org_id: str, repo_id: str, data: bytes
    ) -> ArtifactRef:
        del owner, org_id, repo_id, data
        self.puts += 1
        return ArtifactRef(f"ti/{ORG}/{REPO}/sha256/{DIGEST}", DIGEST, 1)

    async def get(self, ref: ArtifactRef) -> bytes:
        del ref
        self.gets += 1
        return b"blob"


def test_an_unconfigured_sealer_does_not_write() -> None:
    inner = _Memory()
    store = SealedArtifactStore(inner, UnconfiguredSealer())
    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"secret"))
    assert excinfo.value.kind == "encryption_unavailable"
    assert inner.puts == 0


def test_an_unconfigured_sealer_does_not_return_plaintext() -> None:
    inner = _Memory()
    store = SealedArtifactStore(inner, UnconfiguredSealer())
    ref = ArtifactRef(f"ti/{ORG}/{REPO}/sha256/{DIGEST}", DIGEST, 1)
    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(store.get(ref))
    assert excinfo.value.kind == "encryption_unavailable"
    assert inner.gets == 1


def test_retention_selects_nothing_and_refuses_a_duration() -> None:
    keys = (f"exec/{ORG}/{REPO}/sha256/{DIGEST}",)
    assert select_for_deletion(policy="disabled", artifact_keys=keys) == ()
    with pytest.raises(ValueError, match="retention_unconfigured"):
        select_for_deletion(policy="30d", artifact_keys=keys)


def test_the_composition_root_keeps_the_plaintext_store() -> None:
    root = Path(__file__).resolve().parents[3]
    app = (root / "src/flakepilot/interface/composition_root/__init__.py").read_text(
        encoding="utf-8"
    )
    reproduction = (
        root / "src/flakepilot/interface/composition_root/reproduction.py"
    ).read_text(encoding="utf-8")
    assert "LocalFsArtifactStore" in app
    assert "SealedArtifactStore" not in app
    assert "SealedArtifactStore" not in reproduction
    assert "select_for_deletion" not in reproduction
