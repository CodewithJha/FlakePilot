import asyncio
import os
import stat
import uuid
from pathlib import Path

import pytest
from support import InMemoryArtifactStore

from flakepilot.integrations.application.ports import (
    ArtifactRef,
    ArtifactStore,
    ArtifactStoreError,
)
from flakepilot.integrations.infrastructure.artifact_store import LocalFsArtifactStore

ORG, REPO, OTHER = (str(uuid.uuid4()) for _ in range(3))


@pytest.fixture(params=["local_fs", "fake"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactStore:
    if request.param == "local_fs":
        return LocalFsArtifactStore(tmp_path)
    return InMemoryArtifactStore()


# Conformance suite: every artifact adapter and the fake (SYSTEM-ARCHITECTURE §4).


def test_put_returns_a_content_addressed_reference_and_get_returns_the_bytes(
    store: ArtifactStore,
) -> None:
    async def scenario() -> tuple[ArtifactRef, bytes]:
        ref = await store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"<report/>")
        return ref, await store.get(ref)

    ref, data = asyncio.run(scenario())
    assert data == b"<report/>" and ref.size == 9
    assert ref.key == f"ti/{ORG}/{REPO}/sha256/{ref.sha256}"


def test_objects_are_never_overwritten_and_are_namespaced_by_tenant(
    store: ArtifactStore,
) -> None:
    async def scenario() -> list[ArtifactRef]:
        return [
            await store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"one"),
            await store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"one"),
            await store.put(owner="ti", org_id=OTHER, repo_id=REPO, data=b"one"),
        ]

    first, again, other_org = asyncio.run(scenario())
    assert first == again
    assert other_org.key != first.key and other_org.sha256 == first.sha256


@pytest.mark.parametrize(
    "ref",
    [
        ArtifactRef(f"ti/{ORG}/{REPO}/sha256/{'0' * 64}", "0" * 64, 1),
        ArtifactRef(f"ti/{ORG}/{REPO}/../../../../etc/passwd", "0" * 64, 1),
        ArtifactRef("/etc/passwd", "0" * 64, 1),
    ],
    ids=["missing", "traversal", "absolute"],
)
def test_unknown_or_malformed_keys_are_refused(
    store: ArtifactStore, ref: ArtifactRef
) -> None:
    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(store.get(ref))
    assert excinfo.value.kind in ("not_found", "invalid_request")


@pytest.mark.parametrize(
    ("owner", "org", "repo"),
    [("ti", "../x", REPO), ("ti", ORG, ORG.upper()), ("../ti", ORG, REPO)],
)
def test_namespace_parts_are_validated(
    store: ArtifactStore, owner: str, org: str, repo: str
) -> None:
    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(store.put(owner=owner, org_id=org, repo_id=repo, data=b"x"))
    assert excinfo.value.kind == "invalid_request"


def test_a_reference_with_the_wrong_hash_fails_the_integrity_check(
    store: ArtifactStore,
) -> None:
    async def scenario() -> None:
        ref = await store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"data")
        await store.get(ArtifactRef(ref.key, "f" * 64, ref.size))

    with pytest.raises(ArtifactStoreError) as excinfo:
        asyncio.run(scenario())
    assert excinfo.value.kind == "integrity"


# Local filesystem specifics.


def test_tampered_objects_are_detected_on_read_and_on_put(tmp_path: Path) -> None:
    store = LocalFsArtifactStore(tmp_path)
    ref = asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"good"))
    path = tmp_path.joinpath(*ref.key.split("/"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o600)
    path.write_bytes(b"evil")
    for attempt in (
        store.get(ref),
        store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"good"),
    ):
        with pytest.raises(ArtifactStoreError) as excinfo:
            asyncio.run(attempt)
        assert excinfo.value.kind == "integrity"


def test_symlinked_namespaces_cannot_escape_the_root(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "ti").symlink_to(outside)
    store = LocalFsArtifactStore(root)
    with pytest.raises(ArtifactStoreError):
        asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"x"))
    assert list(outside.iterdir()) == []


def test_concurrent_puts_of_the_same_bytes_store_one_object(tmp_path: Path) -> None:
    store = LocalFsArtifactStore(tmp_path)

    async def scenario() -> set[ArtifactRef]:
        refs = await asyncio.gather(
            *(
                store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"same")
                for _ in range(8)
            )
        )
        return set(refs)

    [ref] = asyncio.run(scenario())
    directory = tmp_path.joinpath(*ref.key.split("/")).parent
    assert os.listdir(directory) == [ref.sha256]  # no temporary files left


@pytest.mark.parametrize("root", ["relative/dir", "/nonexistent-flakepilot-dir"])
def test_the_root_must_be_an_existing_absolute_directory(root: str) -> None:
    with pytest.raises(ValueError):
        LocalFsArtifactStore(Path(root))
