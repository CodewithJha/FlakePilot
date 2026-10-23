"""REPO's `CodeHostAccessVerifier` implementation: binding resolution, provider
translation, and fail-closed outcomes. No network and no database here."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from flakepilot.identity.application.verification import AccessVerification
from flakepilot.identity.domain.access import CodeHostAccess
from flakepilot.integrations.application.ports import CodeHostError, CodeHostRole
from flakepilot.repository.application.source import RepoBinding
from flakepilot.repository.application.verification import (
    BoundCodeAccessVerifier,
    DirectoryFactory,
)

ORG = "11111111-2222-4333-8444-555555555555"
OTHER_ORG = "22222222-2222-4333-8444-555555555555"
REPO = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
UNKNOWN = "99999999-2222-4333-8444-555555555555"
BINDING = RepoBinding(ORG, REPO, "acme", "web")


class FakeDirectory:
    def __init__(self, *bindings: RepoBinding) -> None:
        self._bindings = {binding.repo_id: binding for binding in bindings}
        self.lookups: list[str] = []

    async def get(self, repo_id: str) -> RepoBinding | None:
        self.lookups.append(repo_id)
        return self._bindings.get(repo_id)


class RecordingProvider:
    def __init__(
        self,
        role: CodeHostRole | None = None,
        error: CodeHostError | None = None,
    ) -> None:
        self._role = role
        self._error = error
        self.calls: list[tuple[str, str, str]] = []

    async def repository_permission(
        self, *, owner: str, name: str, login: str
    ) -> CodeHostRole:
        self.calls.append((owner, name, login))
        if self._error is not None:
            raise self._error
        assert self._role is not None
        return self._role


def directories(store: FakeDirectory) -> DirectoryFactory:
    @asynccontextmanager
    async def open() -> AsyncIterator[FakeDirectory]:
        yield store

    return open


def verify(
    store: FakeDirectory, provider: Any, *, login: str, repo_id: str, org: str = ORG
) -> AccessVerification:
    return asyncio.run(
        BoundCodeAccessVerifier(directories(store), provider).verify(
            login=login, org_id=org, repo_id=repo_id
        )
    )


def test_a_verified_role_becomes_the_matching_standing() -> None:
    for role, expected in (
        (CodeHostRole.NONE, AccessVerification.NONE),
        (CodeHostRole.READ, AccessVerification.READ),
        (CodeHostRole.WRITE, AccessVerification.WRITE),
        (CodeHostRole.ADMIN, AccessVerification.WRITE),
    ):
        store, provider = FakeDirectory(BINDING), RecordingProvider(role=role)
        assert verify(store, provider, login="member", repo_id=REPO) is expected
        # The lookup used the binding's canonical identity and the given handle.
        assert provider.calls == [("acme", "web", "member")]
        assert store.lookups == [REPO]


def test_only_verified_standings_are_verified() -> None:
    verified = (
        AccessVerification.NONE,
        AccessVerification.READ,
        AccessVerification.WRITE,
    )
    unverified = (
        AccessVerification.UNAVAILABLE,
        AccessVerification.INDETERMINATE,
        AccessVerification.UNBOUND,
    )
    for answer in verified:
        assert answer.is_verified
    for answer in unverified:
        assert not answer.is_verified
    # Every unverified outcome maps to the denying standing.
    for answer in unverified:
        assert answer.standing is CodeHostAccess.NONE


def test_a_foreign_or_unknown_repo_is_unbound_and_never_asks_the_provider() -> None:
    """Non-disclosure: registered under another org, or not registered at all, is the
    same "no canonical identity for this caller" answer."""
    for repo_id in (REPO, UNKNOWN):
        store, provider = (
            FakeDirectory(BINDING),
            RecordingProvider(role=CodeHostRole.ADMIN),
        )
        answer = verify(store, provider, login="member", repo_id=repo_id, org=OTHER_ORG)
        assert answer is AccessVerification.UNBOUND
        assert provider.calls == []  # no provider call without a canonical identity


def test_provider_unavailable_is_not_evidence() -> None:
    for error in (
        CodeHostError("access_unverified", unavailable=True),
        CodeHostError("source_unverified", unavailable=True),
    ):
        store, provider = FakeDirectory(BINDING), RecordingProvider(error=error)
        answer = verify(store, provider, login="member", repo_id=REPO)
        assert answer is AccessVerification.UNAVAILABLE


def test_an_indeterminate_provider_answer_is_not_evidence() -> None:
    store, provider = (
        FakeDirectory(BINDING),
        RecordingProvider(error=CodeHostError("access_unverified", unavailable=False)),
    )
    assert verify(store, provider, login="member", repo_id=REPO) is (
        AccessVerification.INDETERMINATE
    )


def test_an_unusable_handle_is_refused_without_a_provider_call() -> None:
    store, provider = FakeDirectory(BINDING), RecordingProvider(role=CodeHostRole.ADMIN)
    assert verify(store, provider, login="../etc", repo_id=REPO) is (
        AccessVerification.INDETERMINATE
    )
    assert verify(store, provider, login="", repo_id=REPO) is (
        AccessVerification.INDETERMINATE
    )
    assert provider.calls == [] and store.lookups == []


def test_the_verifier_never_sees_or_returns_secrets() -> None:
    store, provider = FakeDirectory(BINDING), RecordingProvider(role=CodeHostRole.READ)
    verifier = BoundCodeAccessVerifier(directories(store), provider)
    answer = asyncio.run(verifier.verify(login="member", org_id=ORG, repo_id=REPO))
    # The result carries the standing only. No token-bearing state ever lands on
    # the result object or on the verifier/provider reprs.
    assert repr(answer) == "<AccessVerification.READ: 'verified_read'>"
    shown = repr(verifier) + repr(provider)
    assert "token" not in shown and "secret" not in shown
