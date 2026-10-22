"""AES-GCM artifact sealing. Keys are generated here and are not production keys.

The composition root still stores plaintext. These tests do not claim encryption
at rest for the deployment.
"""

import asyncio
import secrets
from pathlib import Path

import pytest

from flakepilot.integrations.application.ports import ArtifactRef, ArtifactStoreError
from flakepilot.integrations.application.retention import select_for_deletion
from flakepilot.integrations.infrastructure.aes_gcm import AesGcmSealer
from flakepilot.integrations.infrastructure.artifact_store import LocalFsArtifactStore
from flakepilot.integrations.infrastructure.sealed_store import SealedArtifactStore

ORG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REPO = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SECRET = b"secret-artifact-bytes"
_ROOT = Path(__file__).resolve().parents[3]


def _sealer(current: str, **keys: bytes) -> AesGcmSealer:
    return AesGcmSealer(keys, current)


def test_seal_and_open_round_trip_and_hide_the_plaintext() -> None:
    key = secrets.token_bytes(32)
    sealer = _sealer("k1", k1=key)
    blob = sealer.seal(SECRET)
    assert sealer.open(blob) == SECRET
    assert SECRET not in blob
    assert blob[0] == 1
    assert sealer.open(sealer.seal(b"")) == b""
    assert repr(sealer) == "AesGcmSealer()"
    assert key.hex() not in repr(sealer)


def test_tamper_wrong_key_and_malformed_blobs_fail_closed() -> None:
    key = secrets.token_bytes(32)
    sealer = _sealer("k1", k1=key)
    blob = bytearray(sealer.seal(SECRET))
    blob[-1] ^= 0x01
    with pytest.raises(ArtifactStoreError) as tampered:
        sealer.open(bytes(blob))
    assert tampered.value.kind == "integrity"
    assert SECRET.decode() not in str(tampered.value)
    other = _sealer("k1", k1=secrets.token_bytes(32))
    with pytest.raises(ArtifactStoreError) as wrong:
        other.open(sealer.seal(SECRET))
    assert wrong.value.kind == "integrity"
    with pytest.raises(ArtifactStoreError) as unknown:
        _sealer("k2", k2=key).open(sealer.seal(SECRET))
    assert unknown.value.kind == "integrity"
    with pytest.raises(ArtifactStoreError) as truncated:
        sealer.open(sealer.seal(SECRET)[:8])
    assert truncated.value.kind == "invalid_request"
    with pytest.raises(ArtifactStoreError) as version:
        sealer.open(b"\x02" + sealer.seal(SECRET)[1:])
    assert version.value.kind == "invalid_request"
    with pytest.raises(ArtifactStoreError) as empty:
        sealer.open(b"")
    assert empty.value.kind == "invalid_request"


def test_rotation_keeps_the_retired_key_and_seals_with_the_current_one() -> None:
    old = secrets.token_bytes(32)
    new = secrets.token_bytes(32)
    original = _sealer("v1", v1=old).seal(SECRET)
    rotated = _sealer("v2", v1=old, v2=new)
    assert rotated.open(original) == SECRET
    fresh = rotated.seal(b"after-rotation")
    assert rotated.open(fresh) == b"after-rotation"
    assert fresh[2 : 2 + fresh[1]] == b"v2"
    with pytest.raises(ArtifactStoreError) as missing:
        _sealer("v2", v2=new).open(original)
    assert missing.value.kind == "integrity"


def test_ciphertext_is_what_the_file_stores(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    sealer = _sealer("k1", k1=secrets.token_bytes(32))
    store = SealedArtifactStore(LocalFsArtifactStore(tmp_path), sealer)
    ref = asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=SECRET))
    raw = tmp_path.joinpath(*ref.key.split("/")).read_bytes()
    assert SECRET not in raw
    assert asyncio.run(store.get(ref)) == SECRET
    path = tmp_path.joinpath(*ref.key.split("/"))
    path.chmod(0o600)
    path.write_bytes(raw + b"x")
    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(store.get(ref))
    assert excinfo.value.kind == "integrity"


def test_sealed_puts_reject_symlink_escape_and_traversal(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    root.chmod(0o700)
    (root / "ti").symlink_to(outside)
    store = SealedArtifactStore(
        LocalFsArtifactStore(root),
        _sealer("k1", k1=secrets.token_bytes(32)),
    )
    with pytest.raises(ArtifactStoreError):
        asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=SECRET))
    assert list(outside.iterdir()) == []
    bad = ArtifactRef("../" + ("ab" * 32), "ab" * 32, 1)
    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(store.get(bad))
    assert excinfo.value.kind == "invalid_request"


def test_a_short_key_is_not_a_sealer_and_the_app_does_not_construct_one() -> None:
    with pytest.raises(ArtifactStoreError) as excinfo:
        AesGcmSealer({"k1": b"short"}, "k1")
    assert excinfo.value.kind == "encryption_unavailable"
    app = (_ROOT / "src/flakepilot/interface/composition_root/__init__.py").read_text(
        encoding="utf-8"
    )
    reproduction = (
        _ROOT / "src/flakepilot/interface/composition_root/reproduction.py"
    ).read_text(encoding="utf-8")
    assert "AesGcmSealer" not in app
    assert "AesGcmSealer" not in reproduction
    assert "LocalFsArtifactStore" in app
    reader = reproduction.split("async def read_execution_artifact", 1)[1]
    assert reader.index("record.org_id != org_id") < reader.index("store.get")


def test_retention_still_has_no_approved_policy() -> None:
    with pytest.raises(ValueError, match="retention_unconfigured"):
        select_for_deletion(policy="approved", artifact_keys=())
    assert select_for_deletion(policy="disabled", artifact_keys=()) == ()
