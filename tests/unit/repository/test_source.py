"""A claimed SHA is unverified until the code host returns that same value."""

import asyncio

import pytest

from flakepilot.integrations.application.ports import CodeHostError
from flakepilot.repository.application.source import (
    RepoBinding,
    SourceError,
    pin_revision,
)

ORG = "11111111-1111-1111-1111-111111111111"
REPO = "22222222-2222-2222-2222-222222222222"
SHA = "ab" * 20


class _Directory:
    def __init__(self, binding: RepoBinding | None) -> None:
        self._binding = binding

    async def get(self, repo_id: str) -> RepoBinding | None:
        if self._binding is not None and self._binding.repo_id == repo_id:
            return self._binding
        return None


class _Host:
    def __init__(self, result: str | CodeHostError) -> None:
        self._result = result
        self.calls = 0

    async def confirm_commit(self, *, owner: str, name: str, claimed_sha: str) -> str:
        self.calls += 1
        if isinstance(self._result, CodeHostError):
            raise self._result
        return self._result

    async def fetch_archive(self, *, owner: str, name: str, sha: str) -> bytes:
        return b""


def _binding() -> RepoBinding:
    return RepoBinding(ORG, REPO, "acme", "web")


def test_pin_requires_the_host_to_echo_the_same_sha() -> None:
    host = _Host(SHA)
    pinned = asyncio.run(
        pin_revision(
            _Directory(_binding()), host, org_id=ORG, repo_id=REPO, claimed_sha=SHA
        )
    )
    assert pinned.sha == SHA
    assert host.calls == 1


def test_a_different_host_sha_is_not_evidence() -> None:
    host = _Host("cd" * 20)
    with pytest.raises(SourceError) as excinfo:
        asyncio.run(
            pin_revision(
                _Directory(_binding()),
                host,
                org_id=ORG,
                repo_id=REPO,
                claimed_sha=SHA,
            )
        )
    assert excinfo.value.reason == "unverified_revision"


def test_missing_token_does_not_invent_a_pin() -> None:
    host = _Host(CodeHostError("source_unverified", unavailable=True))
    with pytest.raises(SourceError) as excinfo:
        asyncio.run(
            pin_revision(
                _Directory(_binding()),
                host,
                org_id=ORG,
                repo_id=REPO,
                claimed_sha=SHA,
            )
        )
    assert excinfo.value.reason == "source_unverified"
    assert excinfo.value.unavailable


def test_another_org_and_a_bad_sha_fail_closed() -> None:
    host = _Host(SHA)
    with pytest.raises(SourceError) as missing:
        asyncio.run(
            pin_revision(
                _Directory(_binding()),
                host,
                org_id="33333333-3333-3333-3333-333333333333",
                repo_id=REPO,
                claimed_sha=SHA,
            )
        )
    assert missing.value.reason == "not_found"
    assert host.calls == 0
    with pytest.raises(SourceError) as bad:
        asyncio.run(
            pin_revision(
                _Directory(_binding()),
                host,
                org_id=ORG,
                repo_id=REPO,
                claimed_sha="not-a-sha",
            )
        )
    assert bad.value.reason == "invalid_revision"
