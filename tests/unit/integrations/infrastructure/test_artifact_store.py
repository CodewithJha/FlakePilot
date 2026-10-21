import asyncio
import os
import stat
import time
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


def test_every_directory_level_is_private(tmp_path: Path) -> None:
    previous = os.umask(0o002)  # a permissive umask must not leak through
    try:
        store = LocalFsArtifactStore(tmp_path)
        ref = asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"x"))
    finally:
        os.umask(previous)
    path = tmp_path.joinpath(*ref.key.split("/"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    levels = list(path.relative_to(tmp_path).parents)[:-1]  # every directory below root
    assert len(levels) == 4
    for level in levels:
        assert stat.S_IMODE((tmp_path / level).stat().st_mode) == 0o700, level


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o757])
def test_a_shared_or_foreign_root_is_refused(tmp_path: Path, mode: int) -> None:
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(mode)
    with pytest.raises(ValueError) as excinfo:
        LocalFsArtifactStore(root)
    assert str(root) not in str(excinfo.value)


def test_stale_temporary_files_are_swept_at_startup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "ti" / ORG
    directory.mkdir(parents=True)
    stale, fresh, kept = (directory / n for n in (".tmp-old", ".tmp-new", "object"))
    for path in (stale, fresh, kept):
        path.write_bytes(b"x")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    os.utime(kept, (old, old))
    with caplog.at_level("INFO", logger="flakepilot.artifacts"):
        LocalFsArtifactStore(tmp_path)
    assert (stale.exists(), fresh.exists(), kept.exists()) == (False, True, True)
    assert [r.getMessage() for r in caplog.records] == [
        "removed 1 stale temporary artifact files"
    ]


def test_tampered_objects_are_detected_on_read_and_on_put(tmp_path: Path) -> None:
    store = LocalFsArtifactStore(tmp_path)
    ref = asyncio.run(store.put(owner="ti", org_id=ORG, repo_id=REPO, data=b"good"))
    path = tmp_path.joinpath(*ref.key.split("/"))
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
