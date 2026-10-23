"""The `CodeHostAccessVerifier` port implemented in REPO (DEPENDENCY-RULES §4).

IAM defines the port; this module is REPO's implementation. It combines two facts it
owns the resolution of, and nothing else:

1. the repository binding in `repo.repositories` — the canonical code-host identity an
   operator registered. A binding is a routing fact, never evidence that any user has
   access;
2. the provider's answer for the grantee's account handle on that identity.

It is a pure permission lookup: it never calls IAM authorization, never reads IAM
records (the caller supplies the grantee's handle from IAM), and never reports a
permission it did not receive. Missing binding, ambiguous or failed provider answers
map to the port's unverified outcomes (fail closed; see the port module).
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from flakepilot.identity.application.verification import AccessVerification
from flakepilot.integrations.application.ports import (
    CodeHostError,
    CodeHostRole,
    github_login_ok,
)
from flakepilot.repository.application.source import RepoBinding

__all__ = ["AccessVerification", "BoundCodeAccessVerifier", "PermissionProvider"]


class RepoDirectoryLike(Protocol):
    """Read-only binding lookup, as `SqlRepoDirectory` provides it."""

    async def get(self, repo_id: str) -> RepoBinding | None: ...


class PermissionProvider(Protocol):
    """One code-host permission lookup against a canonical repository identity.
    Implemented by INT's GitHub adapter; answers are roles or `CodeHostError`."""

    async def repository_permission(
        self, *, owner: str, name: str, login: str
    ) -> CodeHostRole: ...


class BoundCodeAccessVerifier:
    """Resolves the binding, asks the provider about the grantee's handle, and reports
    verbatim: a role is a standing; everything else is unverified and denies."""

    def __init__(
        self,
        directories: Callable[[], AbstractAsyncContextManager[RepoDirectoryLike]],
        provider: PermissionProvider,
    ) -> None:
        self._directories = directories
        self._provider = provider

    async def verify(
        self, *, login: str, org_id: str, repo_id: str
    ) -> AccessVerification:
        if not github_login_ok(login):
            # An unusable handle is not a permission fact about any account.
            return AccessVerification.INDETERMINATE
        async with self._directories() as directory:
            binding = await directory.get(repo_id)
        if binding is None or binding.org_id != org_id:
            # The repo is not this org's: unknown identity for this caller. By design
            # this is not distinguishable from "repo unknown to the deployment" here.
            return AccessVerification.UNBOUND
        try:
            role = await self._provider.repository_permission(
                owner=binding.owner, name=binding.name, login=login
            )
        except CodeHostError as error:
            return (
                AccessVerification.UNAVAILABLE
                if error.unavailable
                else AccessVerification.INDETERMINATE
            )
        return {
            CodeHostRole.NONE: AccessVerification.NONE,
            CodeHostRole.READ: AccessVerification.READ,
            CodeHostRole.WRITE: AccessVerification.WRITE,
            CodeHostRole.ADMIN: AccessVerification.WRITE,
        }.get(role, AccessVerification.INDETERMINATE)


DirectoryFactory = Callable[[], AbstractAsyncContextManager[RepoDirectoryLike]]
